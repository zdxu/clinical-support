"""
模块5：差异信号 → 字段变更

将 Phase 2 输出的 FormDiffResult（DiffSignal 列表）转换为 FieldChange 列表。
大部分是纯逻辑转换，只有 append 类型需要一次 LLM 调用来补全字段定义。

还保留：
  load_history_template()   加载模板字段
  list_history_forms()      列出所有模板 Form
  handle_unmatched_forms()  为无模板的新建 Form 生成字段草稿
"""

import json
import time
from pathlib import Path

import yaml
from rich.console import Console

from .models import DiffSignal, FormDiffResult, FieldChange

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
# 新建 Form 字段草稿（无模板可对比时）
# ─────────────────────────────────────────────

def handle_unmatched_forms(
    new_form_names: list,
    form_findings_map: dict,
    client,
) -> list:
    """
    对 Form 映射结果中 is_new=True 的 Form，用 LLM 生成完整字段定义草稿。

    new_form_names: list[str]，需新建的 Form 名称列表
    form_findings_map: dict[str, list]，各 Form 的相关章节文本（key=form_name）

    Returns:
        list[dict]，每个元素含 form_name 和 fields 列表
    """
    results = []

    for form_name in new_form_names:
        context_texts = form_findings_map.get(form_name, [])
        context_str = "\n\n".join(context_texts) if context_texts else "（无 Protocol 描述）"

        prompt = (
            f"根据以下 Protocol 描述，为 {form_name} 生成完整字段定义草稿。\n\n"
            "字段格式参考（仅作示例）：\n"
            '[{"field_name": "VSDAT", "label": "Visit Date", "data_type": "dd MMM yyyy", '
            '"units": "", "values": "", "include_field_oid": "VSDAT", '
            '"source_ref": "第X页", "source_text": "..."}]\n\n'
            f"Protocol 关于此 Form 的描述：\n{context_str}\n\n"
            "输出 JSON 数组，每个字段包含：field_name、label、data_type、units、values、"
            "include_field_oid、source_ref、source_text。"
        )

        for attempt in range(3):
            try:
                response = client.chat.completions.create(
                    model="gpt-4o",
                    max_tokens=4096,
                    messages=[
                        {"role": "system", "content": "你是临床试验 CRF 设计专家，负责起草新 Form 的字段定义。只输出 JSON。"},
                        {"role": "user", "content": prompt},
                    ],
                )
                raw = _strip_code_block(response.choices[0].message.content.strip())
                fields = json.loads(raw)
                results.append({"form_name": form_name, "fields": fields})
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    console.print(f"[red]新建 Form 草稿失败 [{form_name}]：{e}[/red]")
                    results.append({"form_name": form_name, "fields": []})

    return results


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
