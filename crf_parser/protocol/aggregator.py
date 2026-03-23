"""
模块5：归并 + 模板对比

Step A：字段归属归并   assign_findings_to_forms()
Step B：模板对比       compare_with_template()  ← 现在基于 FieldMappingResult 驱动
Step C：新建Form草稿   handle_unmatched_forms()

全部使用纯文本 LLM 调用（无图片）。
"""

import json
import time
from pathlib import Path

import yaml
from rich.console import Console

from .models import FieldFinding, FieldChange, FieldMappingResult, FieldMapping

console = Console()


# ─────────────────────────────────────────────
# Step A：字段归属归并
# ─────────────────────────────────────────────

def assign_findings_to_forms(
    forms_list: list,
    all_findings: list,
    client,
) -> dict:
    """
    将所有 FieldFinding 分配给对应 Form。

    Returns:
        dict[str, list[FieldFinding]]  key="__UNKNOWN__" 用于无法归属的条目
    """
    assigned = {form: [] for form in forms_list}
    assigned["__UNKNOWN__"] = []

    # 策略1：form_hint 非空且 confidence=high → 直接归属
    uncertain = []
    for f in all_findings:
        if f.form_hint and f.confidence == "high":
            target = _fuzzy_match_form(f.form_hint, forms_list)
            if target:
                f.assigned_form = target
                assigned[target].append(f)
            else:
                uncertain.append(f)
        else:
            uncertain.append(f)

    # 策略2：其余统一交给 LLM
    if uncertain:
        llm_results = _llm_assign(uncertain, forms_list, client)
        for f, target in llm_results:
            f.assigned_form = target
            if target in assigned:
                assigned[target].append(f)
            else:
                assigned["__UNKNOWN__"].append(f)

    # 打印归属统计
    console.print("\n[bold cyan]── 字段归属统计 ──[/bold cyan]")
    direct = len(all_findings) - len(uncertain)
    console.print(f"  归属明确 (high form_hint):  {direct:>4} 条")
    console.print(f"  LLM 语义归属:               {len(uncertain):>4} 条")
    console.print(f"  无法归属 (__UNKNOWN__):      {len(assigned['__UNKNOWN__']):>4} 条")

    return assigned


def _fuzzy_match_form(form_hint: str, forms_list: list) -> str:
    """用 rapidfuzz 模糊匹配 Form 名称"""
    try:
        from rapidfuzz import process, fuzz
        result = process.extractOne(
            form_hint,
            forms_list,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=60,
        )
        if result:
            return result[0]
    except ImportError:
        # 简单精确匹配降级
        for f in forms_list:
            if f.lower() == form_hint.lower():
                return f
    return ""


def _llm_assign(uncertain: list, forms_list: list, client) -> list:
    """用 LLM 批量归属字段"""
    findings_json = json.dumps(
        [{"description": f.description, "form_hint": f.form_hint, "source_ref": f.source_ref}
         for f in uncertain],
        ensure_ascii=False,
        indent=2,
    )
    forms_str = "\n".join(f"- {f}" for f in forms_list)

    prompt = (
        f"以下是本研究的 Form 清单：\n{forms_str}\n\n"
        f"以下字段描述需要确定归属（包含 description、form_hint、source_ref）：\n"
        f"{findings_json}\n\n"
        "判断规则：\n"
        "- 参考 form_hint（如果有，优先考虑）\n"
        "- 参考 source_ref 所在章节标题\n"
        "- 以字段语义为主要依据\n"
        "- 实在无法确定归属的填 \"__UNKNOWN__\"\n\n"
        "输出 JSON 数组（与输入顺序对应）：\n"
        "[{\"description\": \"WBC\", \"assigned_form\": \"Hematology\"}, ...]"
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=2048,
                messages=[
                    {"role": "system", "content": "你是临床试验 CRF 设计专家，负责将字段分配到对应的 Form。只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = _strip_code_block(response.choices[0].message.content.strip())
            results = json.loads(raw)

            pairs = []
            for i, item in enumerate(results):
                if i < len(uncertain):
                    pairs.append((uncertain[i], item.get("assigned_form", "__UNKNOWN__")))
            # 处理 LLM 返回数量不足的情况
            for i in range(len(results), len(uncertain)):
                pairs.append((uncertain[i], "__UNKNOWN__"))
            return pairs

        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                console.print(f"[red]LLM 字段归属失败：{e}[/red]")
                return [(f, "__UNKNOWN__") for f in uncertain]


# ─────────────────────────────────────────────
# Step B：模板对比（基于 FieldMappingResult 驱动）
# ─────────────────────────────────────────────

def compare_with_template(
    form_name: str,
    field_mapping_result: FieldMappingResult,
    template_fields: list,
    client,
) -> list:
    """
    基于字段映射结果，生成 FieldChange 列表（exclude / append / override）。

    field_mapping_result: 映射模块B的输出（FieldMappingResult）
    template_fields: 完整模板字段列表（list of dict，从 history YAML 读取）

    Returns:
        list[FieldChange]
    """
    if not template_fields and not field_mapping_result.mappings:
        return []

    changes = []

    # ── exclude 判断：模板有，但 Protocol 未映射到 ──
    matched_field_names = {m.matched_field_name for m in field_mapping_result.matched if m.matched_field_name}
    for template_field in template_fields:
        if template_field.get("deprecated", False):
            continue
        field_name = template_field.get("field_name", "")
        if not field_name:
            continue
        if field_name not in matched_field_names:
            changes.append(FieldChange(
                change_type="exclude",
                field_name=field_name,
                detail={},
                reason=f"Protocol 未提及该字段，可能不需要收集（需人工确认）",
                confidence="low",
                source_ref="",
                source_text="",
                mapped_field_name="",
                mapping_confidence="",
            ))

    # ── append 判断：Protocol 有，模板没有（直接读取 unmatched） ──
    for mapping in field_mapping_result.unmatched:
        detail = _generate_append_detail(mapping, template_fields, client)
        changes.append(FieldChange(
            change_type="append",
            field_name=detail.get("field_name", ""),
            detail=detail,
            reason=mapping.reason or f"Protocol 描述了该字段但模板中未找到对应字段",
            confidence="high",
            source_ref=mapping.source_ref,
            source_text=mapping.source_text,
            mapped_field_name=mapping.matched_field_name,
            mapping_confidence=mapping.confidence,
        ))

    # ── override 判断：字段存在，但属性可能不同 ──
    for mapping in field_mapping_result.matched:
        if not mapping.matched_field_name:
            continue
        template_field = _find_template_field(mapping.matched_field_name, template_fields)
        if not template_field:
            continue

        overrides = _check_override(mapping, template_field, client)
        for attr_diff in overrides:
            changes.append(FieldChange(
                change_type="override",
                field_name=mapping.matched_field_name,
                detail=attr_diff,
                reason=f"Protocol 描述与模板属性存在差异：{attr_diff.get('attribute', '')}",
                confidence="medium",
                source_ref=mapping.source_ref,
                source_text=mapping.source_text,
                mapped_field_name=mapping.matched_field_name,
                mapping_confidence=mapping.confidence,
            ))

    return changes


def compare_with_template_legacy(
    form_name: str,
    form_findings: list,
    template_fields: list,
    client,
) -> list:
    """
    旧版 compare_with_template（直接对比原始字段描述，不使用映射结果）。
    保留兼容：当没有字段映射结果时降级使用。
    """
    if not template_fields and not form_findings:
        return []

    template_summary = json.dumps(
        [{"field_name": f.get("field_name", ""), "label": f.get("label", "")}
         for f in template_fields if not f.get("deprecated", False)],
        ensure_ascii=False,
        indent=2,
    )
    findings_json = json.dumps(
        [f.to_dict() if hasattr(f, "to_dict") else f for f in form_findings],
        ensure_ascii=False,
        indent=2,
    )

    prompt = (
        f"【模板字段列表】\n"
        f"{form_name} 的现有模板字段（field_name + label）：\n"
        f"{template_summary}\n\n"
        f"【Protocol 提取的字段描述】\n"
        f"{findings_json}\n\n"
        "请对比输出差异，分三类：\n"
        "1. exclude：模板有，但 Protocol 未提及\n"
        "2. append：Protocol 有，但模板没有（需要新增字段）\n"
        "3. override：字段存在但属性不同（单位/长度/选项等）\n\n"
        "重要规则：\n"
        "- 模板有但 Protocol 未提及 → confidence=low，不自动 exclude，由人工决定\n"
        "- 只有 Protocol 明确说\"不收集\"才可标注 confidence=high 的 exclude\n"
        "- 每条必须包含 source_ref 和 source_text\n\n"
        "输出 JSON：\n"
        "{\"changes\": [{\"change_type\": \"append\", \"field_name\": \"WEIGHT\", "
        "\"detail\": {\"label\": \"Body Weight\", \"data_type\": \"$5\", \"unit\": \"kg\", "
        "\"values\": \"\", \"include_field_oid\": \"WEIGHT\"}, "
        "\"reason\": \"Protocol 明确要求收集体重\", \"confidence\": \"high\", "
        "\"source_ref\": \"第45页\", \"source_text\": \"Body weight (kg) will be recorded...\"}]}"
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=4096,
                messages=[
                    {"role": "system", "content": "你是临床试验 CRF 设计专家，负责对比 Protocol 和模板字段差异。只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = _strip_code_block(response.choices[0].message.content.strip())
            data = json.loads(raw)

            result = []
            for item in data.get("changes", []):
                result.append(FieldChange(
                    change_type=item.get("change_type", ""),
                    field_name=item.get("field_name", ""),
                    detail=item.get("detail", {}),
                    reason=item.get("reason", ""),
                    confidence=item.get("confidence", "medium"),
                    source_ref=item.get("source_ref", ""),
                    source_text=item.get("source_text", ""),
                ))
            return result

        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                console.print(f"[red]模板对比失败 [{form_name}]：{e}[/red]")
                return []


def _generate_append_detail(mapping: FieldMapping, template_fields: list, client) -> dict:
    """
    用 LLM 根据 Protocol 描述生成 append 字段的完整定义。
    template_fields 用于提供格式参考示例。
    """
    example_field = next(
        (f for f in template_fields if not f.get("deprecated", False)),
        {"field_name": "EXAMPLE", "label": "Example", "data_type": "$200", "unit": "", "values": "", "include_field_oid": ""}
    )
    example_str = json.dumps(example_field, ensure_ascii=False, indent=2)

    prompt = (
        f"根据以下 Protocol 描述，生成该字段的完整定义：\n"
        f"description: {mapping.protocol_description}\n"
        f"source_text: {mapping.source_text or '（无原文）'}\n\n"
        f"参考模板字段格式（仅作格式示例）：\n"
        f"{example_str}\n\n"
        "输出 JSON（仅包含以下字段，不要其他文字）：\n"
        '{"field_name": "", "label": "", "data_type": "", "units": "", "values": "", "include_field_oid": ""}'
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

    # 降级：基于描述生成基础定义
    return {
        "field_name": "",
        "label": mapping.protocol_description,
        "data_type": "$200",
        "units": "",
        "values": "",
        "include_field_oid": "",
    }


def _check_override(mapping: FieldMapping, template_field: dict, client) -> list:
    """
    检查已匹配字段是否有属性差异（单位/长度/选项/条件），返回差异列表。
    """
    field_name = mapping.matched_field_name
    template_unit = template_field.get("units", template_field.get("unit", ""))

    prompt = (
        f"以下是 Protocol 对字段 {field_name} 的描述：\n"
        f"description: {mapping.protocol_description}\n"
        f"source_text: {mapping.source_text or '（无原文）'}\n\n"
        f"以下是模板中该字段的定义：\n"
        f"{json.dumps(template_field, ensure_ascii=False, indent=2)}\n\n"
        "两者是否有需要覆盖的属性差异（单位/长度/选项/条件）？\n"
        "有则输出差异数组，无则输出空数组。\n"
        "输出 JSON：\n"
        '[{"attribute": "units", "protocol_value": "mmHg", "template_value": ""}]'
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=512,
                messages=[
                    {"role": "system", "content": "你是临床试验 CRF 设计专家，负责检查字段属性差异。只输出 JSON 数组。"},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = _strip_code_block(response.choices[0].message.content.strip())
            diffs = json.loads(raw)
            if isinstance(diffs, list):
                return diffs
            return []
        except Exception as e:
            if attempt < 2:
                time.sleep(2)

    return []


def _find_template_field(field_name: str, template_fields: list) -> dict:
    """在模板字段列表中查找指定 field_name 的字段定义"""
    for f in template_fields:
        if f.get("field_name", "") == field_name:
            return f
    return {}


# ─────────────────────────────────────────────
# Step C：新建 Form 字段草稿
# ─────────────────────────────────────────────

def handle_unmatched_forms(
    extracted_forms: list,
    history_forms: list,
    form_findings_map: dict,
    client,
) -> list:
    """
    对在 history 中找不到模板的 Form，用 LLM 生成完整字段定义草稿。

    Returns:
        list[dict]  每个元素包含 form_name 和 fields 列表
    """
    results = []
    unmatched = [f for f in extracted_forms if _fuzzy_match_form(f, history_forms) == ""]

    for form_name in unmatched:
        findings = form_findings_map.get(form_name, [])
        findings_json = json.dumps(
            [f.to_dict() for f in findings],
            ensure_ascii=False,
            indent=2,
        )

        prompt = (
            f"根据以下 Protocol 描述，为 {form_name} 生成完整字段定义。\n\n"
            "参考字段格式（仅作格式示例）：\n"
            '[{"field_name": "VSDAT", "label": "Visit Date", "data_type": "dd MMM yyyy", '
            '"unit": "", "values": "", "include_field_oid": "VSDAT"}]\n\n'
            f"Protocol 关于此 Form 的描述：\n{findings_json}\n\n"
            "为每个字段输出：field_name、label、data_type、unit、values、include_field_oid、"
            "confidence、source_ref、source_text\n\n"
            "输出 JSON 数组。"
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
# 加载历史模板
# ─────────────────────────────────────────────

def load_history_template(form_name: str, history_dir: str) -> list:
    """
    从 history 目录加载指定 Form 的模板字段。

    Returns:
        list[dict]，每个 dict 为一个字段定义；找不到则返回 []
    """
    import re

    history_path = Path(history_dir)
    if not history_path.exists():
        return []

    # 将 form_name 转换为文件名
    name = form_name.lower()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    name = name.strip("_")
    yaml_file = history_path / f"{name}.yaml"

    if not yaml_file.exists():
        # 尝试模糊匹配
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


def _fuzzy_match_file(form_name: str, candidates: list) -> Path:
    """用 rapidfuzz 模糊匹配文件名"""
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
