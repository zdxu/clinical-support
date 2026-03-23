"""
Protocol → CRF 信息提取 Pipeline 主入口

用法：
  # 完整运行（PDF）
  python protocol/main.py --input input/protocol/ADG138-Protocol.pdf

  # 完整运行（docx）
  python protocol/main.py --input input/protocol/ADG138-Protocol.docx

  # 指定兜底 TOC
  python protocol/main.py --input ... --toc input/protocol/toc.json

  # 只跑 Phase 1，先确认 Form 清单
  python protocol/main.py --input ... --phase 1

  # Phase 1 已确认，只跑 Phase 2 + 归并
  python protocol/main.py --input ... --phase 2

  # PDF 专用：跳过截图，使用缓存
  python protocol/main.py --input ... --use-cache
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()

# 确保 crf_parser 根目录在 sys.path
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def main():
    parser = argparse.ArgumentParser(description="Protocol → CRF 信息提取 Pipeline")
    parser.add_argument("--input", required=True, help="输入 Protocol 文件（PDF 或 docx）")
    parser.add_argument("--toc", default=None, help="兜底 TOC JSON 文件路径")
    parser.add_argument("--phase", type=int, default=0, choices=[0, 1, 2],
                        help="运行阶段：0=完整, 1=只Phase1, 2=Phase2+归并")
    parser.add_argument("--use-cache", action="store_true", help="PDF 截图使用缓存")
    parser.add_argument("--output-base", default="output/protocol", help="输出根目录")
    parser.add_argument("--crf-history", default="output/crf/history", help="CRF history 模板库目录")
    parser.add_argument("--tmp-dir", default="tmp", help="截图缓存目录")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        console.print(f"[red]错误：找不到文件 {input_path}[/red]")
        sys.exit(1)

    ext = input_path.suffix.lower()
    if ext not in (".pdf", ".docx"):
        console.print("[red]错误：仅支持 .pdf 和 .docx 格式[/red]")
        sys.exit(1)

    output_base = Path(args.output_base)
    output_base.mkdir(parents=True, exist_ok=True)

    console.print(f"\n[bold]Protocol → CRF 信息提取 Pipeline[/bold]")
    console.print(f"输入文件: [cyan]{input_path}[/cyan]")
    console.print(f"文件类型: [cyan]{ext}[/cyan]\n")

    # ── 初始化 OpenAI client ──
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or api_key == "your_api_key_here":
        console.print("[red]错误：请在 .env 中设置 OPENAI_API_KEY[/red]")
        sys.exit(1)

    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    # ── Step a: TOC ──
    console.print("[bold cyan]── TOC 获取 ──[/bold cyan]")
    from protocol.toc_extractor import get_toc
    chapters = get_toc(
        input_file=str(input_path),
        toc_json_path=args.toc,
        client=client,
        tmp_dir=args.tmp_dir,
    )
    if not chapters:
        console.print("[red]错误：TOC 获取失败，无法继续[/red]")
        sys.exit(1)
    console.print(f"共识别 [bold]{len(chapters)}[/bold] 个章节\n")

    # ── Step b: PDF 截图 ──
    image_paths = []
    if ext == ".pdf":
        console.print("[bold cyan]── PDF 截图 ──[/bold cyan]")
        from parser.pdf_to_images import pdf_to_images
        image_paths = pdf_to_images(str(input_path), tmp_dir=args.tmp_dir)
        console.print(f"[green]截图完成[/green]：共 {len(image_paths)} 页\n")

    # ── Step c: 提取 study_info ──
    console.print("[bold cyan]── 研究基本信息提取 ──[/bold cyan]")
    if ext == ".docx":
        from protocol.docx_extractor import extract_study_info_from_docx
        study_info = extract_study_info_from_docx(str(input_path), client)
    else:
        from protocol.docx_extractor import extract_study_info_from_pdf
        study_info = extract_study_info_from_pdf(image_paths[:5], client)

    study_info_path = output_base / "study_info.json"
    with open(study_info_path, "w", encoding="utf-8") as f:
        json.dump(study_info.to_dict(), f, ensure_ascii=False, indent=2)
    console.print(f"  project_name: [cyan]{study_info.project_name}[/cyan]")
    console.print(f"  phase:        [cyan]{study_info.phase}[/cyan]")
    console.print(f"  indication:   [cyan]{study_info.indication}[/cyan]")
    console.print(f"  → {study_info_path}\n")

    # ── Step d: Phase 1 ──
    from protocol.phase1_form_scanner import extract_forms_list, FIXED_FORMS
    from protocol.aggregator import list_history_forms

    history_forms = list_history_forms(args.crf_history)
    console.print(f"[dim]history 模板库：{len(history_forms)} 个 Form[/dim]")

    if args.phase in (0, 1):
        extracted_forms = extract_forms_list(
            chapters=chapters,
            docx_path=str(input_path) if ext == ".docx" else None,
            image_paths=image_paths if ext == ".pdf" else None,
            file_type=ext.lstrip("."),
            client=client,
        )

        # 与 history 匹配
        _print_form_match(extracted_forms, history_forms)

        # 保存 forms_list.json
        forms_list_path = output_base / "forms_list.json"
        all_forms = FIXED_FORMS + extracted_forms
        with open(forms_list_path, "w", encoding="utf-8") as f:
            json.dump({
                "fixed_forms": FIXED_FORMS,
                "extracted_forms": extracted_forms,
                "all_forms": all_forms,
            }, f, ensure_ascii=False, indent=2)
        console.print(f"\n[green]Form 清单已保存：{forms_list_path}[/green]")

        if args.phase == 1:
            console.print("\n[yellow]--phase 1 模式，结束。请人工确认 forms_list.json 后运行 --phase 2[/yellow]")
            return
    else:
        # --phase 2：从已保存的 forms_list.json 读取
        forms_list_path = output_base / "forms_list.json"
        if not forms_list_path.exists():
            console.print("[red]错误：找不到 forms_list.json，请先运行 --phase 1[/red]")
            sys.exit(1)
        with open(forms_list_path, "r", encoding="utf-8") as f:
            forms_data = json.load(f)
        extracted_forms = forms_data.get("extracted_forms", [])
        all_forms = forms_data.get("all_forms", FIXED_FORMS + extracted_forms)
        console.print(f"[cyan]读取已确认 Form 清单：{len(all_forms)} 个[/cyan]")

    # ── Step e: Phase 2 ──
    from protocol.phase2_field_scanner import extract_all_field_findings
    all_findings = extract_all_field_findings(
        chapters=chapters,
        docx_path=str(input_path) if ext == ".docx" else None,
        image_paths=image_paths if ext == ".pdf" else None,
        file_type=ext.lstrip("."),
        client=client,
    )

    # 按章节保存原始结果
    _save_findings_by_form(all_findings, output_base)

    # ── Step f: 归并 ──
    from protocol.aggregator import assign_findings_to_forms
    all_form_names = FIXED_FORMS + extracted_forms
    form_findings_map = assign_findings_to_forms(all_form_names, all_findings, client)

    # ── Step g: 模板对比 ──
    from protocol.aggregator import compare_with_template, load_history_template
    console.print("\n[bold cyan]── 模板对比 ──[/bold cyan]")

    from protocol.models import ProtocolExtraction
    extraction = ProtocolExtraction(
        study_info=study_info,
        fixed_forms=FIXED_FORMS,
        extracted_forms=extracted_forms,
    )

    for form_name in extracted_forms:
        findings = form_findings_map.get(form_name, [])
        template_fields = load_history_template(form_name, args.crf_history)

        if template_fields:
            changes = compare_with_template(form_name, findings, template_fields, client)
            extraction.field_changes[form_name] = changes
            exc = sum(1 for c in changes if c.change_type == "exclude")
            app = sum(1 for c in changes if c.change_type == "append")
            ovr = sum(1 for c in changes if c.change_type == "override")
            console.print(f"  {form_name:<30} → exclude:{exc}  append:{app}  override:{ovr}")

            # 保存 field_changes
            changes_dir = output_base / "field_changes"
            changes_dir.mkdir(exist_ok=True)
            import re
            fn = re.sub(r"[^a-z0-9]+", "_", form_name.lower()).strip("_")
            changes_path = changes_dir / f"{fn}.json"
            with open(changes_path, "w", encoding="utf-8") as f:
                json.dump([c.to_dict() for c in changes], f, ensure_ascii=False, indent=2)

    # 未知字段
    extraction.unknown_findings = form_findings_map.get("__UNKNOWN__", [])

    # ── Step h: 新建 Form 草稿 ──
    from protocol.aggregator import handle_unmatched_forms
    console.print("\n[bold cyan]── 新建 Form 字段草稿 ──[/bold cyan]")
    study_specific = handle_unmatched_forms(
        extracted_forms=extracted_forms,
        history_forms=history_forms,
        form_findings_map=form_findings_map,
        client=client,
    )
    extraction.study_specific_forms = study_specific

    # 保存
    ss_dir = output_base / "study_specific_forms"
    ss_dir.mkdir(exist_ok=True)
    for draft in study_specific:
        import re
        fn = re.sub(r"[^a-z0-9]+", "_", draft["form_name"].lower()).strip("_")
        with open(ss_dir / f"{fn}.json", "w", encoding="utf-8") as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)
        console.print(f"  {draft['form_name']} → {len(draft.get('fields', []))} 个字段草稿")

    # ── Step i: 审核表 ──
    console.print("\n[bold cyan]── 生成审核表 ──[/bold cyan]")
    from protocol.checklist_writer import generate_checklist
    checklist_path = generate_checklist(
        extraction=extraction,
        history_forms=history_forms,
        output_dir=str(output_base),
    )

    # ── Step j: 保存汇总 ──
    extraction_path = output_base / "protocol_extraction.json"
    with open(extraction_path, "w", encoding="utf-8") as f:
        json.dump(extraction.to_dict(), f, ensure_ascii=False, indent=2)

    # ── 最终汇总 ──
    console.print(f"\n[bold cyan]── 最终汇总 ──[/bold cyan]")
    console.print(f"  Form 清单：固定 {len(FIXED_FORMS)} + 提取 {len(extracted_forms)} = {len(FIXED_FORMS) + len(extracted_forms)} 个")
    console.print(f"  字段描述：{len(all_findings)} 条")
    console.print(f"  未知字段：{len(extraction.unknown_findings)} 条")
    console.print(f"  新建Form：{len(study_specific)} 个")
    console.print(f"\n[green]完成！[/green]")
    console.print(f"  审核表: [cyan]{checklist_path}[/cyan]")
    console.print(f"  汇总:   [cyan]{extraction_path}[/cyan]")


def _print_form_match(extracted_forms: list, history_forms: list):
    """打印 Form 与模板库的匹配情况"""
    from protocol.aggregator import _fuzzy_match_form
    console.print("\n[bold cyan]── Form 与模板库匹配 ──[/bold cyan]")
    matched = 0
    unmatched = 0
    for form in extracted_forms:
        m = _fuzzy_match_form(form, history_forms)
        if m:
            console.print(f"  [green]✓[/green] {form:<30} → 匹配: {m}")
            matched += 1
        else:
            console.print(f"  [yellow]○[/yellow] {form:<30} → 无模板（将新建）")
            unmatched += 1
    console.print(f"\n  来源B Form：{len(extracted_forms)} 个 | 模板匹配：{matched} 个 | 未匹配（新建）：{unmatched} 个")


def _save_findings_by_form(all_findings: list, output_base: Path):
    """将原始字段描述按 form_hint 分类保存"""
    import re
    findings_dir = output_base / "field_findings"
    findings_dir.mkdir(exist_ok=True)

    by_form = {}
    for f in all_findings:
        key = f.form_hint if f.form_hint else "__no_hint__"
        by_form.setdefault(key, []).append(f.to_dict())

    for form_name, items in by_form.items():
        fn = re.sub(r"[^a-z0-9]+", "_", form_name.lower()).strip("_") or "no_hint"
        with open(findings_dir / f"{fn}.json", "w", encoding="utf-8") as f:
            import json
            json.dump(items, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
