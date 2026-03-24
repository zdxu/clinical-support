"""
模块4：Phase 2 差异信号提取

以模板字段列表为基准，从 Protocol 章节中找出相对于模板的差异描述。
Protocol 没有提到的模板字段 → 默认保留，不生成信号。
只有 Protocol 有明确原文依据才生成差异信号。

输出：list[DiffSignal]，类型为 exclude / append / override / condition
"""

import base64
import json
import time
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

from .models import Chapter, DiffSignal, FormDiffResult, FormMappingResult

console = Console()


_SYSTEM_PROMPT = (
    "你是临床试验数据管理专家。\n\n"
    "我会给你两个输入：\n"
    "1. 一个 CRF Form 的模板字段列表（这是数据采集的基准）\n"
    "2. Protocol 中与该 Form 相关的章节内容\n\n"
    "任务：以模板字段列表为基准，从 Protocol 中找出差异信号。\n\n"
    "差异信号的定义：\n"
    "- exclude：Protocol 明确说明不收集某个模板字段\n"
    '  例："Temperature will not be assessed in this study"\n'
    "  → 对应模板字段 TEMP 的 exclude 信号\n\n"
    "- append：Protocol 要求收集模板中不存在的额外数据\n"
    '  例："Body weight and height will be recorded to calculate BMI"\n'
    "  → 模板没有 WEIGHT/HEIGHT/BMI，生成 append 信号\n\n"
    "- override：Protocol 对某个模板字段有特殊规格要求\n"
    '  例："Blood pressure will be measured in mmHg after 5 minutes of rest"\n'
    "  → 如果模板 SYSBP 没有填写单位，生成 override 信号（units: mmHg）\n\n"
    "- condition：某字段的收集有特殊适用条件\n"
    '  例："Pregnancy test required for females of childbearing potential only"\n'
    "  → 生成 condition 信号\n\n"
    "重要规则：\n"
    "- Protocol 没有提到的模板字段 → 不生成任何信号（默认保留）\n"
    "- 只有 Protocol 有明确原文描述才生成信号，不要推断或猜测\n"
    "- source_text 必须是原文引用（1-2句），禁止改写或总结\n"
    "- 不要生成空信号或重复信号\n\n"
    "confidence 判断：\n"
    '- "high"   = 原文明确，无歧义\n'
    '- "medium" = 原文暗示，需确认\n'
    '- "low"    = 原文模糊，不确定\n\n'
    "只输出 JSON，不要有其他文字：\n"
    '{"diff_signals": [\n'
    '  {"diff_type": "exclude", "field_ref": "TEMP", "detail": {}, "confidence": "high",\n'
    '   "source_ref": "第47页", "source_text": "Temperature will not be assessed in this study."},\n'
    '  {"diff_type": "append", "field_ref": "WEIGHT",\n'
    '   "detail": {"description": "Body Weight", "unit": "kg", "condition": ""},\n'
    '   "confidence": "high", "source_ref": "第46页",\n'
    '   "source_text": "Body weight (kg) will be recorded at each scheduled visit."},\n'
    '  {"diff_type": "override", "field_ref": "SYSBP",\n'
    '   "detail": {"attribute": "units", "from": "", "to": "mmHg"},\n'
    '   "confidence": "medium", "source_ref": "第45页",\n'
    '   "source_text": "Blood pressure will be measured in mmHg."},\n'
    '  {"diff_type": "condition", "field_ref": "PREG",\n'
    '   "detail": {"condition": "Females of childbearing potential only"},\n'
    '   "confidence": "high", "source_ref": "第52页",\n'
    '   "source_text": "Pregnancy test is required for females of childbearing potential only."}\n'
    "]}"
)


def _build_template_summary(template_fields: list) -> str:
    """将模板字段列表格式化为 LLM 可读的表格字符串"""
    lines = ["field_name | label | data_type | unit"]
    lines.append("-" * 60)
    for f in template_fields:
        if f.get("deprecated", False):
            continue
        fn = f.get("field_name", "")
        label = f.get("label", "")
        dt = f.get("data_type", "")
        unit = f.get("units", f.get("unit", ""))
        lines.append(f"{fn} | {label} | {dt} | {unit}")
    return "\n".join(lines)


def extract_diff_signals(
    form_name: str,
    template_fields: list,
    chapters: list,
    docx_path: str = None,
    image_paths: list = None,
    file_type: str = "pdf",
    client=None,
) -> list:
    """
    以模板字段为基准，从 Protocol 相关章节中提取差异信号。

    form_name: Form 名称（上下文）
    template_fields: 该 Form 对应模板的完整字段列表（list[dict]）
    chapters: 与该 Form 相关的章节列表（list[Chapter]）
    docx_path / image_paths: 文档内容来源
    file_type: "pdf" 或 "docx"
    client: OpenAI client

    Returns:
        list[DiffSignal]
    """
    if not template_fields:
        return []

    template_summary = _build_template_summary(template_fields)

    # 预先建立 PDF 截图的页码映射
    page_image_map = {}
    if file_type == "pdf" and image_paths:
        for path in image_paths:
            stem = Path(path).stem
            try:
                page_num = int(stem.split("_")[-1])
                page_image_map[page_num] = path
            except ValueError:
                pass

    for attempt in range(3):
        try:
            if file_type == "pdf":
                signals = _extract_pdf(
                    form_name, template_summary, chapters, page_image_map, client
                )
            else:
                signals = _extract_docx(
                    form_name, template_summary, chapters, docx_path, client
                )
            return signals

        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                console.print(f"[red]差异信号提取失败 [{form_name}]：{e}[/red]")
                return []

    return []


def _extract_docx(
    form_name: str,
    template_summary: str,
    chapters: list,
    docx_path: str,
    client,
) -> list:
    """docx 路径：拼接所有相关章节全文，单次 LLM 调用"""
    from .docx_extractor import extract_chapter_content

    chapter_texts = []
    for chapter in chapters:
        if docx_path:
            content = extract_chapter_content(docx_path, chapter)
            chapter_texts.append(f"=== {chapter.title} ===\n{content.full_text}")
        else:
            chapter_texts.append(f"=== {chapter.title} ===\n（无内容）")

    chapters_text = "\n\n".join(chapter_texts)
    chapter_titles = ", ".join(c.title for c in chapters)

    user_prompt = (
        f"Form 名称：{form_name}\n\n"
        "【模板字段列表（基准）】\n"
        "以下是该 Form 的现有模板字段，请以此为基准寻找差异：\n"
        f"{template_summary}\n\n"
        "【Protocol 相关章节内容（文本）】\n"
        f"章节：{chapter_titles}\n\n"
        f"{chapters_text}"
    )

    return _call_llm(user_prompt, client)


def _extract_pdf(
    form_name: str,
    template_summary: str,
    chapters: list,
    page_image_map: dict,
    client,
) -> list:
    """PDF 路径：收集所有相关章节图片，带页码，单次 LLM 调用"""
    img_blocks = []
    page_refs = []
    chapter_titles = []

    for chapter in chapters:
        chapter_titles.append(chapter.title)
        if chapter.page_range:
            start, end = chapter.page_range
            for page_num in range(start, end + 1):
                if page_num in page_image_map:
                    path = page_image_map[page_num]
                    try:
                        with open(path, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode("utf-8")
                        img_blocks.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        })
                        page_refs.append(f"第{page_num}页")
                    except Exception:
                        pass

    text_block = {
        "type": "text",
        "text": (
            f"Form 名称：{form_name}\n\n"
            "【模板字段列表（基准）】\n"
            "以下是该 Form 的现有模板字段，请以此为基准寻找差异：\n"
            f"{template_summary}\n\n"
            "【Protocol 相关章节内容（图片）】\n"
            f"章节：{', '.join(chapter_titles)}\n"
            f"图片页码：{', '.join(page_refs)}"
        ),
    }

    user_content = img_blocks + [text_block]
    return _call_llm(user_content, client)


def _call_llm(user_content, client) -> list:
    """执行 LLM 调用，解析并返回 list[DiffSignal]"""
    if isinstance(user_content, str):
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
    else:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=4096,
        messages=messages,
    )
    raw = _strip_code_block(response.choices[0].message.content.strip())
    data = json.loads(raw)

    signals = []
    for item in data.get("diff_signals", []):
        signals.append(DiffSignal(
            diff_type=item.get("diff_type", ""),
            field_ref=item.get("field_ref", ""),
            detail=item.get("detail", {}),
            confidence=item.get("confidence", "medium"),
            source_ref=item.get("source_ref", ""),
            source_text=item.get("source_text", ""),
        ))
    return signals


def _group_signals(form_name: str, template_file: str, signals: list) -> FormDiffResult:
    """将 DiffSignal 列表按类型分组，生成 FormDiffResult"""
    result = FormDiffResult(
        form_name=form_name,
        template_file=template_file,
        diff_signals=signals,
    )
    for s in signals:
        dt = s.diff_type
        if dt == "exclude":
            result.excludes.append(s)
        elif dt == "append":
            result.appends.append(s)
        elif dt == "override":
            result.overrides.append(s)
        elif dt == "condition":
            result.conditions.append(s)
    return result


def extract_all_diff_signals(
    form_mapping_result: FormMappingResult,
    chapters: list,
    history_dir: str,
    docx_path: str = None,
    image_paths: list = None,
    file_type: str = "pdf",
    client=None,
) -> dict:
    """
    对每个已匹配模板的 Form 执行差异信号提取。
    一对多 Form（如 Laboratory Tests → 多个模板）：对每个模板分别提取，结果合并。
    新建 Form / low 置信度映射跳过。

    Returns:
        dict[str, FormDiffResult]，key 为 protocol_form 名称
    """
    results = {}

    # 预先建立 PDF 截图页码映射
    page_image_map = {}
    if file_type == "pdf" and image_paths:
        for path in image_paths:
            stem = Path(path).stem
            try:
                page_num = int(stem.split("_")[-1])
                page_image_map[page_num] = path
            except ValueError:
                pass

    console.print("\n[bold cyan]── Phase 2：差异信号提取 ──[/bold cyan]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Form", style="cyan", width=28)
    table.add_column("exclude", justify="right", width=8)
    table.add_column("append", justify="right", width=8)
    table.add_column("override", justify="right", width=9)
    table.add_column("condition", justify="right", width=10)

    # 处理 high + medium 中有模板匹配的 Form
    candidates = [m for m in (form_mapping_result.high + form_mapping_result.medium)
                  if not m.is_new and m.matched_templates]

    for mapping in candidates:
        form_name = mapping.protocol_form
        relevant_chapters = _get_relevant_chapters(form_name, chapters)

        all_signals = []
        primary_template = mapping.matched_templates[0]

        for template_file in mapping.matched_templates:
            template_fields = _load_template_fields(template_file, history_dir)
            if not template_fields:
                continue

            signals = extract_diff_signals(
                form_name=form_name,
                template_fields=template_fields,
                chapters=relevant_chapters,
                docx_path=docx_path,
                image_paths=image_paths,
                file_type=file_type,
                client=client,
            )
            all_signals.extend(signals)

        form_diff = _group_signals(form_name, primary_template, all_signals)
        results[form_name] = form_diff

        table.add_row(
            form_name[:28],
            str(len(form_diff.excludes)),
            str(len(form_diff.appends)),
            str(len(form_diff.overrides)),
            str(len(form_diff.conditions)),
        )

    console.print(table)
    total = sum(len(r.diff_signals) for r in results.values())
    console.print(f"共提取差异信号：[bold]{total}[/bold] 条\n")

    return results


def save_diff_signals(all_results: dict, output_base: str) -> dict:
    """将差异信号结果保存到 output/protocol/diff_signals/{form_name}.json"""
    import re
    signals_dir = Path(output_base) / "diff_signals"
    signals_dir.mkdir(parents=True, exist_ok=True)

    saved = {}
    for form_name, result in all_results.items():
        fn = re.sub(r"[^a-z0-9]+", "_", form_name.lower()).strip("_") or "unknown"
        out_path = signals_dir / f"{fn}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        saved[form_name] = str(out_path)

    console.print(f"[green]差异信号已保存到：{signals_dir}[/green]")
    return saved


def load_diff_signals(form_name: str, output_base: str) -> FormDiffResult:
    """从文件读取指定 Form 的差异信号结果"""
    import re
    fn = re.sub(r"[^a-z0-9]+", "_", form_name.lower()).strip("_") or "unknown"
    path = Path(output_base) / "diff_signals" / f"{fn}.json"
    if not path.exists():
        return FormDiffResult(form_name=form_name)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    def _parse(item: dict) -> DiffSignal:
        return DiffSignal(
            diff_type=item.get("diff_type", ""),
            field_ref=item.get("field_ref", ""),
            detail=item.get("detail", {}),
            confidence=item.get("confidence", "medium"),
            source_ref=item.get("source_ref", ""),
            source_text=item.get("source_text", ""),
        )

    signals = [_parse(s) for s in data.get("diff_signals", [])]
    return FormDiffResult(
        form_name=data.get("form_name", form_name),
        template_file=data.get("template_file", ""),
        diff_signals=signals,
        excludes=[_parse(s) for s in data.get("excludes", [])],
        appends=[_parse(s) for s in data.get("appends", [])],
        overrides=[_parse(s) for s in data.get("overrides", [])],
        conditions=[_parse(s) for s in data.get("conditions", [])],
    )


def _get_relevant_chapters(form_name: str, chapters: list) -> list:
    """
    从全部章节中筛选与该 Form 相关的章节。
    策略：标题中含 Form 名关键词，或全部返回（兜底）。
    """
    form_lower = form_name.lower()
    keywords = form_lower.split()

    matched = [
        c for c in chapters
        if any(kw in c.title.lower() for kw in keywords if len(kw) > 3)
    ]
    # 兜底：如果没有匹配到，返回全部章节（让 LLM 自己判断）
    return matched if matched else chapters


def _load_template_fields(template_file: str, history_dir: str) -> list:
    """读取模板文件，返回字段列表"""
    template_path = Path(history_dir) / template_file
    if not template_path.exists():
        console.print(f"[yellow]警告：模板文件不存在 {template_path}[/yellow]")
        return []
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("fields", [])
    except Exception as e:
        console.print(f"[yellow]警告：读取模板失败 {template_file}：{e}[/yellow]")
        return []


def _strip_code_block(raw: str) -> str:
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw
