"""
Protocol → CRF 信息提取 Pipeline 主入口

用法：
  # 完整运行（PDF）
  python protocol/main.py --input input/protocol/ADG138-Protocol.pdf

  # 完整运行（docx）
  python protocol/main.py --input input/protocol/ADG138-Protocol.docx

  # 指定兜底 TOC
  python protocol/main.py --input ... --toc input/protocol/toc.json

  # 只跑 Phase 1 + Form 级映射，先确认 Form 与模板的映射关系
  python protocol/main.py --input ... --phase 1

  # Phase 1 已确认，只跑 Phase 2（差异信号提取）+ 归并
  python protocol/main.py --input ... --phase 2

  # PDF 专用：跳过截图，使用缓存
  python protocol/main.py --input ... --use-cache
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def main():
    parser = argparse.ArgumentParser(description="Protocol → CRF 信息提取 Pipeline")
    parser.add_argument("--input", required=True, help="输入 Protocol 文件（PDF 或 docx）")
    parser.add_argument("--toc", default=None, help="兜底 TOC JSON 文件路径")
    parser.add_argument("--phase", type=int, default=0, choices=[0, 1, 2],
                        help="运行阶段：0=完整, 1=Phase1+Form映射（等待人工确认）, 2=Phase2+差异信号+归并")
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

    # ── Step d: Phase 1 - Form 清单 ──
    from protocol.phase1_form_scanner import extract_forms_list, FIXED_FORMS
    from protocol.aggregator import list_history_forms

    history_forms = list_history_forms(args.crf_history)
    console.print(f"[dim]history 模板库：{len(history_forms)} 个 Form[/dim]")

    if args.phase in (0, 1):
        extracted_form_findings = extract_forms_list(
            chapters=chapters,
            docx_path=str(input_path) if ext == ".docx" else None,
            image_paths=image_paths if ext == ".pdf" else None,
            file_type=ext.lstrip("."),
            client=client,
        )
        extracted_forms = [ff.form_name for ff in extracted_form_findings]

        forms_list_path = output_base / "forms_list.json"
        with open(forms_list_path, "w", encoding="utf-8") as f:
            json.dump({
                "fixed_forms": FIXED_FORMS,
                "extracted_forms": extracted_forms,
                "all_forms": FIXED_FORMS + extracted_forms,
                "extracted_form_findings": [ff.to_dict() for ff in extracted_form_findings],
            }, f, ensure_ascii=False, indent=2)
        console.print(f"\n[green]Form 清单已保存：{forms_list_path}[/green]")

        # ── Step e: Form 级语义映射 ──
        console.print("\n[bold cyan]── Form 级语义映射 ──[/bold cyan]")
        from protocol.form_mapper import build_template_summary, map_forms, save_form_mapping

        template_summaries = build_template_summary(args.crf_history)
        console.print(f"[dim]模板库摘要：{len(template_summaries)} 个模板[/dim]")

        form_mapping_result = map_forms(
            protocol_forms=extracted_forms,
            template_summaries=template_summaries,
            client=client,
        )
        save_form_mapping(form_mapping_result, str(output_base))

        if args.phase == 1:
            console.print(
                "\n[yellow]--phase 1 模式，结束。"
                "请人工确认 forms_list.json 和 form_mapping.json 后运行 --phase 2[/yellow]"
            )
            return

    else:
        # --phase 2：从已保存的文件读取
        forms_list_path = output_base / "forms_list.json"
        if not forms_list_path.exists():
            console.print("[red]错误：找不到 forms_list.json，请先运行 --phase 1[/red]")
            sys.exit(1)
        with open(forms_list_path, "r", encoding="utf-8") as f:
            forms_data = json.load(f)
        extracted_forms = forms_data.get("extracted_forms", [])
        extracted_form_findings = _load_form_findings(output_base)
        console.print(f"[cyan]读取已确认 Form 清单：{len(extracted_forms)} 个[/cyan]")

        from protocol.form_mapper import load_form_mapping, build_template_summary, map_forms, save_form_mapping
        form_mapping_result = load_form_mapping(str(output_base))
        if not form_mapping_result.mappings:
            console.print("[yellow]未找到 Form 映射结果，重新执行 Form 级映射...[/yellow]")
            template_summaries = build_template_summary(args.crf_history)
            form_mapping_result = map_forms(
                protocol_forms=extracted_forms,
                template_summaries=template_summaries,
                client=client,
            )
            save_form_mapping(form_mapping_result, str(output_base))

    # ── Step f: Phase 2 - 差异信号提取 ──
    from protocol.phase2_field_scanner import (
        extract_all_diff_signals, save_diff_signals
    )

    all_diff_results = extract_all_diff_signals(
        form_mapping_result=form_mapping_result,
        chapters=chapters,
        history_dir=args.crf_history,
        docx_path=str(input_path) if ext == ".docx" else None,
        image_paths=image_paths if ext == ".pdf" else None,
        file_type=ext.lstrip("."),
        client=client,
    )
    save_diff_signals(all_diff_results, str(output_base))

    # ── Step g: 差异信号 → 字段变更 ──
    from protocol.aggregator import diff_to_field_changes, load_history_template
    console.print("\n[bold cyan]── 字段变更生成 ──[/bold cyan]")

    from protocol.models import ProtocolExtraction
    extraction = ProtocolExtraction(
        study_info=study_info,
        fixed_forms=FIXED_FORMS,
        extracted_forms=extracted_forms,
        extracted_form_findings=extracted_form_findings,
    )

    for form_name, form_diff in all_diff_results.items():
        template_fields = _load_template_for_form(form_name, form_mapping_result, args.crf_history)
        changes = diff_to_field_changes(form_diff, template_fields, client)
        extraction.field_changes[form_name] = changes

        exc = sum(1 for c in changes if c.change_type == "exclude")
        app = sum(1 for c in changes if c.change_type == "append")
        ovr = sum(1 for c in changes if c.change_type == "override")
        console.print(f"  {form_name:<30} → exclude:{exc}  append:{app}  override:{ovr}")

        changes_dir = output_base / "field_changes"
        changes_dir.mkdir(exist_ok=True)
        fn = re.sub(r"[^a-z0-9]+", "_", form_name.lower()).strip("_")
        with open(changes_dir / f"{fn}.json", "w", encoding="utf-8") as f:
            json.dump([c.to_dict() for c in changes], f, ensure_ascii=False, indent=2)

    # ── Step h: 新建 Form 字段草稿 ──
    from protocol.aggregator import handle_unmatched_forms
    console.print("\n[bold cyan]── 新建 Form 字段草稿 ──[/bold cyan]")

    new_form_names = [m.protocol_form for m in form_mapping_result.new_forms]
    if new_form_names:
        # 收集新建 Form 相关的章节文本，作为 LLM 生成草稿的上下文
        new_form_context = _collect_chapter_texts(new_form_names, chapters, ext,
                                                   str(input_path) if ext == ".docx" else None)
        study_specific = handle_unmatched_forms(new_form_names, new_form_context, client)
        extraction.study_specific_forms = study_specific

        ss_dir = output_base / "study_specific_forms"
        ss_dir.mkdir(exist_ok=True)
        for draft in study_specific:
            fn = re.sub(r"[^a-z0-9]+", "_", draft["form_name"].lower()).strip("_")
            with open(ss_dir / f"{fn}.json", "w", encoding="utf-8") as f:
                json.dump(draft, f, ensure_ascii=False, indent=2)
            console.print(f"  {draft['form_name']} → {len(draft.get('fields', []))} 个字段草稿")
    else:
        console.print("  （无需新建 Form）")

    # ── Step i: 审核表 ──
    console.print("\n[bold cyan]── 生成审核表 ──[/bold cyan]")
    from protocol.checklist_writer import generate_checklist
    checklist_path = generate_checklist(
        extraction=extraction,
        history_forms=history_forms,
        output_dir=str(output_base),
        form_mapping_result=form_mapping_result,
    )

    # ── Step j: 保存汇总 ──
    extraction_path = output_base / "protocol_extraction.json"
    with open(extraction_path, "w", encoding="utf-8") as f:
        json.dump(extraction.to_dict(), f, ensure_ascii=False, indent=2)

    # ── 最终汇总 ──
    total_changes = sum(len(v) for v in extraction.field_changes.values())
    console.print(f"\n[bold cyan]── 最终汇总 ──[/bold cyan]")
    console.print(f"  Form 清单：固定 {len(FIXED_FORMS)} + 提取 {len(extracted_forms)} = {len(FIXED_FORMS) + len(extracted_forms)} 个")
    console.print(f"  差异信号：{sum(len(r.diff_signals) for r in all_diff_results.values())} 条")
    console.print(f"  字段变更：{total_changes} 条")
    console.print(f"  新建Form：{len(extraction.study_specific_forms)} 个")
    console.print(
        f"  Form映射：high:{len(form_mapping_result.high)}  "
        f"medium:{len(form_mapping_result.medium)}  "
        f"low:{len(form_mapping_result.low)}  "
        f"新建:{len(form_mapping_result.new_forms)}"
    )
    console.print(f"\n[green]完成！[/green]")
    console.print(f"  审核表: [cyan]{checklist_path}[/cyan]")
    console.print(f"  汇总:   [cyan]{extraction_path}[/cyan]")


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _load_form_findings(output_base: Path) -> list:
    """从 forms_list.json 读取 extracted_form_findings（list[FormFinding]）"""
    from protocol.models import FormFinding
    forms_list_path = output_base / "forms_list.json"
    if not forms_list_path.exists():
        return []
    with open(forms_list_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [
        FormFinding(
            form_name=item.get("form_name", ""),
            source_ref=item.get("source_ref", ""),
            source_text=item.get("source_text", ""),
        )
        for item in data.get("extracted_form_findings", [])
    ]


def _load_template_for_form(form_name: str, form_mapping_result, history_dir: str) -> list:
    """根据 Form 映射结果加载模板字段，一对多时合并所有模板字段。"""
    import yaml

    matched_templates = []
    for m in form_mapping_result.mappings:
        if m.protocol_form == form_name and not m.is_new:
            matched_templates = m.matched_templates
            break

    if matched_templates:
        all_fields = []
        for tfile in matched_templates:
            tpath = Path(history_dir) / tfile
            if tpath.exists():
                try:
                    with open(tpath, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    all_fields.extend(data.get("fields", []))
                except Exception:
                    pass
        if all_fields:
            return all_fields

    from protocol.aggregator import load_history_template
    return load_history_template(form_name, history_dir)


def _collect_chapter_texts(form_names: list, chapters: list, file_type: str, docx_path: str) -> dict:
    """为新建 Form 收集相关章节文本，返回 dict[form_name, list[str]]"""
    result = {name: [] for name in form_names}

    for form_name in form_names:
        keywords = form_name.lower().split()
        matched = [
            c for c in chapters
            if any(kw in c.title.lower() for kw in keywords if len(kw) > 3)
        ]
        if not matched:
            matched = chapters  # 兜底

        for chapter in matched:
            if file_type == "docx" and docx_path:
                try:
                    from protocol.docx_extractor import extract_chapter_content
                    content = extract_chapter_content(docx_path, chapter)
                    result[form_name].append(f"=== {chapter.title} ===\n{content.full_text}")
                except Exception:
                    pass

    return result


if __name__ == "__main__":
    main()
