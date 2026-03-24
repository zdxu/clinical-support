"""
模块5：差异信号 → 字段变更 + 新建 Form 字段草稿

matched Form 路径：
  diff_to_field_changes()  将 FormDiffResult 转换为 FieldChange 列表（主要是纯逻辑）

unmatched Form 路径（三种情况）：
  handle_new_forms()
    情况A：基于标准工具（RECIST/ECOG/量表等）→ LLM 行业知识生成字段
    情况B：有 Protocol 章节描述 → 从章节提取字段列表
    情况C：无任何描述 → 返回空字段列表，进入审核表供 DM 手填
"""

import json
import time
from pathlib import Path

import yaml
from rich.console import Console

import base64

from .models import DiffSignal, FormDiffResult, FieldChange, FormMapping, Chapter

console = Console()


# ─────────────────────────────────────────────
# 核心：DiffSignal → FieldChange 转换
# ─────────────────────────────────────────────

def diff_to_field_changes(
    form_diff_result: FormDiffResult,
    template_fields: list,
    client,
) -> list:
    """
    将 FormDiffResult 中的 DiffSignal 列表转换为 FieldChange 列表。
    纯逻辑转换 + append 时调用一次 LLM 补全字段定义。

    template_fields: 完整模板字段列表（用于 exclude 的上下文 + append 的格式参考）

    Returns:
        list[FieldChange]
    """
    changes = []

    # 1. exclude 信号 → FieldChange(exclude)
    #    detail 填入模板字段上下文，帮助 DM 判断是否真的不收集
    for signal in form_diff_result.excludes:
        template_field = _find_template_field(signal.field_ref, template_fields)
        changes.append(FieldChange(
            change_type="exclude",
            field_name=signal.field_ref,
            detail={
                "label": template_field.get("label", ""),
                "data_type": template_field.get("data_type", ""),
                "units": template_field.get("units", template_field.get("unit", "")),
            },
            reason=signal.source_text,
            confidence=signal.confidence,
            source_ref=signal.source_ref,
            source_text=signal.source_text,
        ))

    # 2. append 信号 → FieldChange(append)
    #    调用 LLM 根据 DiffSignal.detail 补全完整字段定义
    for signal in form_diff_result.appends:
        field_def = _generate_field_def(signal, template_fields, client)
        changes.append(FieldChange(
            change_type="append",
            field_name=field_def.get("field_name", signal.field_ref),
            detail=field_def,
            reason=signal.source_text,
            confidence=signal.confidence,
            source_ref=signal.source_ref,
            source_text=signal.source_text,
        ))

    # 3. override 信号 → FieldChange(override)
    for signal in form_diff_result.overrides:
        changes.append(FieldChange(
            change_type="override",
            field_name=signal.field_ref,
            detail=signal.detail,
            reason=signal.source_text,
            confidence=signal.confidence,
            source_ref=signal.source_ref,
            source_text=signal.source_text,
        ))

    # 4. condition 信号 → FieldChange(override，修改 notes 属性)
    for signal in form_diff_result.conditions:
        changes.append(FieldChange(
            change_type="override",
            field_name=signal.field_ref,
            detail={
                "attribute": "notes",
                "value": signal.detail.get("condition", ""),
            },
            reason=signal.source_text,
            confidence=signal.confidence,
            source_ref=signal.source_ref,
            source_text=signal.source_text,
        ))

    # 模板有但无任何信号的字段 → 默认保留，不生成变更

    return changes


def _generate_field_def(signal: DiffSignal, template_fields: list, client) -> dict:
    """
    根据 append 类型 DiffSignal 的描述，用 LLM 生成完整字段定义。
    取前2个模板字段作为格式示例。
    """
    example_fields = [f for f in template_fields if not f.get("deprecated", False)][:2]
    example_str = json.dumps(example_fields, ensure_ascii=False, indent=2)

    desc = signal.detail.get("description", signal.field_ref)
    unit = signal.detail.get("unit", "")
    condition = signal.detail.get("condition", "")

    prompt = (
        f"根据以下描述，生成新字段的完整定义。\n\n"
        f"字段描述：{desc}\n"
        f"单位：{unit}\n"
        f"条件：{condition}\n"
        f"原文：{signal.source_text or '（无原文）'}\n\n"
        "参考以下模板字段的格式（仅作格式示例，不要照搬内容）：\n"
        f"{example_str}\n\n"
        "输出 JSON（不要其他文字）：\n"
        '{"field_name": "", "label": "", "data_type": "", '
        '"units": "", "values": "", "include_field_oid": ""}'
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=512,
                messages=[
                    {"role": "system", "content": "你是临床试验 CRF 设计专家，负责定义字段。只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = _strip_code_block(response.choices[0].message.content.strip())
            return json.loads(raw)
        except Exception as e:
            if attempt < 2:
                time.sleep(2)

    # 降级：返回基础定义
    return {
        "field_name": signal.field_ref,
        "label": desc,
        "data_type": "$200",
        "units": unit,
        "values": "",
        "include_field_oid": signal.field_ref,
    }


# ─────────────────────────────────────────────
# 新建 Form 字段草稿（三种情况）
# ─────────────────────────────────────────────

def handle_new_forms(
    new_form_mappings: list,
    chapters: list,
    docx_path: str = None,
    image_paths: list = None,
    file_type: str = "pdf",
    client=None,
) -> list:
    """
    处理模板库中不存在的新建 Form（FormMapping.is_new=True）。

    三种情况：
      情况A：基于标准工具（RECIST/ECOG/量表等）→ LLM 用行业知识生成字段
      情况B：有 Protocol 章节描述 → 从相关章节提取字段列表
      情况C：无任何描述 → 返回空字段列表，进入审核表供 DM 手填

    new_form_mappings: list[FormMapping]，is_new=True 的映射结果
    chapters: list[Chapter]，全部章节
    docx_path / image_paths: 文档内容来源
    file_type: "pdf" 或 "docx"
    client: OpenAI client

    Returns:
        list[dict]，每个元素含：
          form_name, situation("A"/"B"/"C"), standard(str|None), fields(list)
    """
    results = []

    # 预建 PDF 页码映射
    page_image_map = {}
    if file_type == "pdf" and image_paths:
        for path in image_paths:
            stem = Path(path).stem
            try:
                page_image_map[int(stem.split("_")[-1])] = path
            except ValueError:
                pass

    for mapping in new_form_mappings:
        form_name = mapping.protocol_form
        console.print(f"\n  [cyan]{form_name}[/cyan]")

        relevant = _get_relevant_chapters_for_new_form(form_name, chapters)

        # ── 先判断是否基于标准工具 ──
        standard = _detect_standard_tool(form_name, relevant, docx_path, file_type, client)

        if standard:
            # 情况A：标准工具
            console.print(f"    情况A：标准工具 [{standard}]")
            fields = _generate_fields_from_standard(
                form_name, standard, relevant, docx_path, page_image_map, file_type, client
            )
            results.append({
                "form_name": form_name,
                "situation": "A",
                "standard": standard,
                "fields": fields,
            })

        elif relevant:
            # 情况B：有 Protocol 章节描述
            console.print(f"    情况B：从章节提取（{len(relevant)} 个相关章节）")
            fields = _extract_fields_from_protocol(
                form_name, relevant, docx_path, page_image_map, file_type, client
            )
            results.append({
                "form_name": form_name,
                "situation": "B",
                "standard": None,
                "fields": fields,
            })

        else:
            # 情况C：无任何描述
            console.print(f"    情况C：无章节描述，生成空模板")
            results.append({
                "form_name": form_name,
                "situation": "C",
                "standard": None,
                "fields": [],
            })

        count = len(results[-1]["fields"])
        label = {"A": "标准工具", "B": "章节提取", "C": "空模板（DM手填）"}[results[-1]["situation"]]
        console.print(f"    → 情况{results[-1]['situation']}（{label}）：{count} 个字段草稿")

    return results


def _detect_standard_tool(
    form_name: str,
    relevant_chapters: list,
    docx_path: str,
    file_type: str,
    client,
) -> str:
    """
    判断该 Form 是否基于某个标准工具（RECIST/ECOG/量表等）。
    返回标准工具名称（如 "RECIST 1.1"），或空字符串表示不是。
    """
    if not relevant_chapters and not form_name:
        return ""

    # 从相关章节取最多 2000 字的上下文
    context = _get_chapter_context(relevant_chapters, docx_path, file_type, max_chars=2000)

    prompt = (
        f"Form 名称：{form_name}\n\n"
        f"Protocol 相关章节片段：\n{context or '（无章节内容）'}\n\n"
        "问题：该 Form 的数据收集是否基于某个行业标准工具或量表？\n"
        "（如 RECIST、ECOG、CTCAE、PGIC、SF-36、NRS、PHQ-9 等）\n\n"
        "如果是，输出标准工具名称（例如 \"RECIST 1.1\"）。\n"
        "如果不是，输出空字符串 \"\"。\n\n"
        "只输出 JSON：{\"standard\": \"RECIST 1.1\"} 或 {\"standard\": \"\"}"
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=128,
                messages=[
                    {"role": "system", "content": "你是临床试验专家。只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = _strip_code_block(response.choices[0].message.content.strip())
            return json.loads(raw).get("standard", "")
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
    return ""


def _generate_fields_from_standard(
    form_name: str,
    standard: str,
    relevant_chapters: list,
    docx_path: str,
    page_image_map: dict,
    file_type: str,
    client,
) -> list:
    """情况A：用 LLM 行业知识生成基于标准工具的字段定义"""
    context = _get_chapter_context(relevant_chapters, docx_path, file_type, max_chars=3000)

    prompt = (
        f"请根据 {standard} 生成 {form_name} 的完整标准字段定义。\n\n"
        f"Protocol 上下文（作为参考）：\n{context or '（无）'}\n\n"
        "输出 JSON 数组，每个字段包含：\n"
        "field_name（大写）、label、data_type（如 $3/$5/Y/N/dd MMM yyyy）、"
        "units、values（枚举值用逗号分隔，无则空）、include_field_oid、"
        "source_ref（填 \"行业标准\" 或具体章节）、source_text（标准原文或协议引用）。\n\n"
        "confidence 规则：\n"
        "- \"high\"   = 标准规范明确定义的字段\n"
        "- \"medium\" = 标准隐含，Protocol 未明确列出\n"
        "- \"low\"    = 不确定，需人工决定\n\n"
        "每个字段还需包含 confidence 字段。只输出 JSON 数组。"
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=4096,
                messages=[
                    {"role": "system", "content": "你是临床试验 CRF 设计专家，熟悉各类行业标准工具。只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = _strip_code_block(response.choices[0].message.content.strip())
            return json.loads(raw)
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                console.print(f"[red]情况A字段生成失败 [{form_name}]：{e}[/red]")
    return []


def _extract_fields_from_protocol(
    form_name: str,
    relevant_chapters: list,
    docx_path: str,
    page_image_map: dict,
    file_type: str,
    client,
) -> list:
    """情况B：从 Protocol 章节文本/图片中提取字段列表"""
    system_prompt = (
        "你是临床试验 CRF 设计专家。\n"
        f"这是一个在历史模板库中不存在的新 Form：{form_name}。\n"
        "请从 Protocol 描述中提取所有需要收集的字段，生成完整字段定义。\n\n"
        "输出 JSON 数组，每个字段包含：\n"
        "field_name（大写建议命名）、label、data_type、units、values、"
        "include_field_oid、confidence（high/medium/low）、source_ref、source_text。\n"
        "只输出 JSON 数组，不要其他文字。"
    )

    if file_type == "pdf":
        # 收集图片
        img_blocks = []
        page_refs = []
        for chapter in relevant_chapters:
            if chapter.page_range:
                for pg in range(chapter.page_range[0], chapter.page_range[1] + 1):
                    if pg in page_image_map:
                        try:
                            with open(page_image_map[pg], "rb") as f:
                                b64 = base64.b64encode(f.read()).decode()
                            img_blocks.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{b64}"},
                            })
                            page_refs.append(f"第{pg}页")
                        except Exception:
                            pass
        user_content = img_blocks + [{
            "type": "text",
            "text": f"Form 名称：{form_name}\n章节：{', '.join(c.title for c in relevant_chapters)}\n页码：{', '.join(page_refs)}",
        }]
    else:
        text = _get_chapter_context(relevant_chapters, docx_path, file_type)
        user_content = f"Form 名称：{form_name}\n\n{text}"

    for attempt in range(3):
        try:
            messages = [{"role": "system", "content": system_prompt}]
            if isinstance(user_content, str):
                messages.append({"role": "user", "content": user_content})
            else:
                messages.append({"role": "user", "content": user_content})

            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=4096,
                messages=messages,
            )
            raw = _strip_code_block(response.choices[0].message.content.strip())
            return json.loads(raw)
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                console.print(f"[red]情况B字段提取失败 [{form_name}]：{e}[/red]")
    return []


_GENERIC_WORDS = {
    "assessment", "assessments", "evaluation", "evaluations",
    "test", "tests", "testing", "procedure", "procedures",
    "form", "forms", "visit", "visits", "data", "collection",
    "findings", "finding", "result", "results", "measurement",
}

def _get_relevant_chapters_for_new_form(form_name: str, chapters: list) -> list:
    """筛选与新建 Form 名称相关的章节（关键词匹配，过滤通用词）"""
    raw_keywords = [kw for kw in form_name.lower().split() if len(kw) > 3]
    keywords = [kw for kw in raw_keywords if kw not in _GENERIC_WORDS]
    if not keywords:
        keywords = raw_keywords  # 全是通用词时降级使用原词
    matched = [c for c in chapters if any(kw in c.title.lower() for kw in keywords)]
    return matched  # 不做兜底（情况C就是没有匹配）


def _get_chapter_context(
    chapters: list,
    docx_path: str,
    file_type: str,
    max_chars: int = 5000,
) -> str:
    """从章节列表拼接纯文本上下文（仅 docx 有效，PDF 返回空）"""
    if file_type != "docx" or not docx_path or not chapters:
        return ""

    parts = []
    total = 0
    for chapter in chapters:
        try:
            from .docx_extractor import extract_chapter_content
            content = extract_chapter_content(docx_path, chapter)
            text = f"=== {chapter.title} ===\n{content.full_text}"
            parts.append(text)
            total += len(text)
            if total >= max_chars:
                break
        except Exception:
            pass
    return "\n\n".join(parts)[:max_chars]


# ─────────────────────────────────────────────
# 模板加载工具
# ─────────────────────────────────────────────

def load_history_template(form_name: str, history_dir: str) -> list:
    """
    从 history 目录加载指定 Form 的模板字段（按文件名模糊匹配）。
    优先使用 form_mapper 的映射结果；此函数作为降级兜底。

    Returns:
        list[dict]；找不到则返回 []
    """
    import re

    history_path = Path(history_dir)
    if not history_path.exists():
        return []

    name = re.sub(r"[^a-z0-9]+", "_", form_name.lower()).strip("_")
    yaml_file = history_path / f"{name}.yaml"

    if not yaml_file.exists():
        candidates = list(history_path.glob("*.yaml"))
        best_match = _fuzzy_match_file(form_name, candidates)
        if best_match:
            yaml_file = best_match
        else:
            return []

    with open(yaml_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("fields", [])


def list_history_forms(history_dir: str) -> list:
    """返回 history 目录中所有 Form 名称"""
    history_path = Path(history_dir)
    if not history_path.exists():
        return []

    forms = []
    for yaml_file in sorted(history_path.glob("*.yaml")):
        if yaml_file.name.startswith("_"):
            continue
        with open(yaml_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        form_name = data.get("form_name", yaml_file.stem)
        forms.append(form_name)

    return forms


# ─────────────────────────────────────────────
# 内部工具
# ─────────────────────────────────────────────

def _find_template_field(field_name: str, template_fields: list) -> dict:
    for f in template_fields:
        if f.get("field_name", "") == field_name:
            return f
    return {}


def _fuzzy_match_form(form_hint: str, forms_list: list) -> str:
    try:
        from rapidfuzz import process, fuzz
        result = process.extractOne(
            form_hint, forms_list,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=60,
        )
        if result:
            return result[0]
    except ImportError:
        for f in forms_list:
            if f.lower() == form_hint.lower():
                return f
    return ""


def _fuzzy_match_file(form_name: str, candidates: list) -> Path:
    try:
        from rapidfuzz import process, fuzz
        names = [p.stem for p in candidates]
        result = process.extractOne(
            form_name.lower().replace(" ", "_"),
            names,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=60,
        )
        if result:
            idx = names.index(result[0])
            return candidates[idx]
    except ImportError:
        pass
    return None


def _strip_code_block(raw: str) -> str:
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw
