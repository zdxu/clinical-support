"""
映射模块 B：字段级 LLM 语义映射

将 Protocol 提取的字段描述（FieldFinding）映射到模板字段定义（field_name）。
支持一对多 Form（如 Laboratory Tests → 多个 Lab Form），分别对每个模板执行映射后合并。
"""

import json
import re
import time
from pathlib import Path

import yaml
from rich.console import Console

from .models import FieldFinding, FieldMapping, FieldMappingResult, FormMappingResult

console = Console()


def build_field_summary(template_file: str, history_dir: str) -> list:
    """
    读取指定模板文件，返回字段摘要列表供 LLM 匹配。

    Returns:
        list[dict]，每个 dict 含 field_name、label、data_type、unit
    """
    template_path = Path(history_dir) / template_file
    if not template_path.exists():
        console.print(f"[yellow]警告：模板文件不存在 {template_path}[/yellow]")
        return []

    try:
        with open(template_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return [
            {
                "field_name": field.get("field_name", ""),
                "label": field.get("label", ""),
                "data_type": field.get("data_type", ""),
                "unit": field.get("units", field.get("unit", "")),
            }
            for field in data.get("fields", [])
            if not field.get("deprecated", False)
        ]
    except Exception as e:
        console.print(f"[yellow]警告：读取模板字段失败 {template_file}：{e}[/yellow]")
        return []


def map_fields(
    form_name: str,
    protocol_findings: list,
    template_fields: list,
    client,
    template_file: str = "",
) -> FieldMappingResult:
    """
    使用 LLM 将 Protocol 字段描述语义映射到模板字段。

    form_name: Form 名称（用于 prompt 上下文）
    protocol_findings: Phase 2 该 Form 的字段描述列表（list[FieldFinding]）
    template_fields: build_field_summary() 的输出（list[dict]）
    client: OpenAI client
    template_file: 模板文件名（用于标注来源）

    Returns:
        FieldMappingResult
    """
    if not protocol_findings:
        return FieldMappingResult(form_name=form_name)

    findings_json = json.dumps(
        [
            {
                "description": f.description if hasattr(f, "description") else f.get("description", ""),
                "unit": f.unit if hasattr(f, "unit") else f.get("unit", ""),
                "condition": f.condition if hasattr(f, "condition") else f.get("condition", ""),
                "source_ref": f.source_ref if hasattr(f, "source_ref") else f.get("source_ref", ""),
                "source_text": f.source_text if hasattr(f, "source_text") else f.get("source_text", ""),
            }
            for f in protocol_findings
        ],
        ensure_ascii=False,
        indent=2,
    )
    template_fields_json = json.dumps(template_fields, ensure_ascii=False, indent=2)

    system_prompt = (
        "你是临床试验数据管理专家。\n"
        "任务：将 Protocol 中的字段描述映射到模板字段定义。\n\n"
        "映射规则：\n"
        "1. 语义完全一致（含缩写、单位、修饰词变体） → confidence=high\n"
        "   例：\"Systolic Blood Pressure (mmHg)\" → SYSBP（Systolic Blood Pressure）\n"
        "   例：\"HR\" → HR（Heart Rate）\n"
        "   例：\"Body Wt\" → WEIGHT（Body Weight）\n"
        "2. 语义相近但有歧义 → confidence=medium\n"
        "3. 模板中明确没有对应字段 → is_new=true，matched_field_name=\"\"，confidence=high\n"
        "4. 无法判断 → confidence=low\n\n"
        "只输出 JSON，不要有其他文字。"
    )

    user_prompt = (
        f"Form 名称：{form_name}"
        + (f"（模板文件：{template_file}）" if template_file else "")
        + "\n\n"
        f"【Protocol 提取的字段描述】\n"
        f"{findings_json}\n"
        "（每条含 description、unit、condition、source_ref、source_text）\n\n"
        f"【模板字段列表】\n"
        f"{template_fields_json}\n"
        "（每条含 field_name、label、data_type、unit）\n\n"
        "请为每条 Protocol 字段描述输出映射结果，格式：\n"
        '[\n'
        '  {\n'
        '    "protocol_description": "Systolic Blood Pressure (mmHg)",\n'
        '    "matched_field_name": "SYSBP",\n'
        '    "is_new": false,\n'
        '    "confidence": "high",\n'
        '    "reason": "语义完全一致，均指收缩压，单位 mmHg 一致",\n'
        '    "source_ref": "第45页",\n'
        '    "source_text": "Systolic and diastolic blood pressure..."\n'
        '  },\n'
        '  {\n'
        '    "protocol_description": "Body Weight",\n'
        '    "matched_field_name": "",\n'
        '    "is_new": true,\n'
        '    "confidence": "high",\n'
        '    "reason": "模板中无体重字段，需新增",\n'
        '    "source_ref": "第46页",\n'
        '    "source_text": "Body weight (kg) will be recorded at each visit."\n'
        '  }\n'
        ']'
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=4096,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            raw = _strip_code_block(response.choices[0].message.content.strip())
            items = json.loads(raw)

            # 构建映射结果，保留溯源信息
            findings_lookup = {
                (f.description if hasattr(f, "description") else f.get("description", "")): f
                for f in protocol_findings
            }

            mappings = []
            for item in items:
                desc = item.get("protocol_description", "")
                src_finding = findings_lookup.get(desc)
                src_ref = item.get("source_ref", "")
                src_text = item.get("source_text", "")
                if src_finding and not src_ref:
                    src_ref = src_finding.source_ref if hasattr(src_finding, "source_ref") else src_finding.get("source_ref", "")
                if src_finding and not src_text:
                    src_text = src_finding.source_text if hasattr(src_finding, "source_text") else src_finding.get("source_text", "")

                mappings.append(FieldMapping(
                    protocol_description=desc,
                    matched_field_name=item.get("matched_field_name", ""),
                    is_new=item.get("is_new", False),
                    confidence=item.get("confidence", "medium"),
                    reason=item.get("reason", ""),
                    source_ref=src_ref,
                    source_text=src_text,
                ))

            result = _group_field_mappings(form_name, mappings)
            return result

        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                console.print(f"[red]字段映射失败 [{form_name}]：{e}[/red]")
                # 降级：所有字段标记为 low 置信度
                mappings = [
                    FieldMapping(
                        protocol_description=(
                            f.description if hasattr(f, "description") else f.get("description", "")
                        ),
                        matched_field_name="",
                        is_new=False,
                        confidence="low",
                        reason=f"LLM 调用失败：{e}",
                        source_ref=(
                            f.source_ref if hasattr(f, "source_ref") else f.get("source_ref", "")
                        ),
                        source_text=(
                            f.source_text if hasattr(f, "source_text") else f.get("source_text", "")
                        ),
                    )
                    for f in protocol_findings
                ]
                return _group_field_mappings(form_name, mappings)

    return FieldMappingResult(form_name=form_name)


def _group_field_mappings(form_name: str, mappings: list) -> FieldMappingResult:
    """按 is_new 和 confidence 分组"""
    result = FieldMappingResult(form_name=form_name, mappings=mappings)

    for m in mappings:
        if m.confidence == "low":
            result.ambiguous.append(m)
        elif m.is_new:
            result.unmatched.append(m)
        else:
            result.matched.append(m)

    return result


def map_all_fields(
    form_mapping_result: FormMappingResult,
    field_findings: dict,
    history_dir: str,
    client,
) -> dict:
    """
    对每个已匹配到模板的 Form，执行字段级映射。

    form_mapping_result: 映射模块 A 的输出
    field_findings: dict[str, list[FieldFinding]]，Phase 2 输出（按 Form 分组）
    history_dir: history 模板库目录
    client: OpenAI client

    Returns:
        dict[str, FieldMappingResult]，key 为 protocol_form 名称
    """
    all_results = {}

    # 收集所有有模板匹配的 Form（high + medium 中 is_new=False 的）
    forms_to_map = [
        m for m in form_mapping_result.mappings
        if not m.is_new and m.matched_templates
    ]

    console.print("\n[bold cyan]── 字段级映射 ──[/bold cyan]")

    for form_mapping in forms_to_map:
        form_name = form_mapping.protocol_form
        findings = _get_findings_for_form(form_name, field_findings)

        if not findings:
            console.print(f"  [dim]{form_name:<30} → 无字段描述，跳过[/dim]")
            continue

        # 一对多：对每个模板分别执行映射，然后合并
        if len(form_mapping.matched_templates) == 1:
            template_file = form_mapping.matched_templates[0]
            template_fields = build_field_summary(template_file, history_dir)
            result = map_fields(form_name, findings, template_fields, client, template_file)
        else:
            result = _map_fields_multi_template(
                form_name, findings, form_mapping.matched_templates, history_dir, client
            )

        all_results[form_name] = result

        _print_field_mapping_summary(form_name, result)

    return all_results


def _map_fields_multi_template(
    form_name: str,
    findings: list,
    template_files: list,
    history_dir: str,
    client,
) -> FieldMappingResult:
    """
    一对多 Form 映射：对多个模板分别执行字段映射，合并结果。
    Protocol 字段只要在任意一个模板中匹配到，即视为 matched。
    """
    # 合并所有模板的字段（标注来源模板）
    all_template_fields = []
    for tfile in template_files:
        fields = build_field_summary(tfile, history_dir)
        for field in fields:
            field["_template_file"] = tfile
        all_template_fields.extend(fields)

    if not all_template_fields:
        return FieldMappingResult(form_name=form_name)

    result = map_fields(
        form_name,
        findings,
        all_template_fields,
        client,
        template_file=", ".join(template_files),
    )
    return result


def _get_findings_for_form(form_name: str, field_findings: dict) -> list:
    """
    从 field_findings 字典中查找指定 Form 的字段描述。
    支持大小写不敏感和 assigned_form 字段匹配。
    """
    # 直接精确匹配
    if form_name in field_findings:
        return field_findings[form_name]

    # 大小写不敏感匹配
    form_lower = form_name.lower()
    for key, findings in field_findings.items():
        if key.lower() == form_lower:
            return findings

    return []


def _print_field_mapping_summary(form_name: str, result: FieldMappingResult):
    """打印字段映射摘要"""
    console.print(
        f"  {form_name:<30} → "
        f"matched:[green]{len(result.matched)}[/green]  "
        f"append:[cyan]{len(result.unmatched)}[/cyan]  "
        f"ambiguous:[yellow]{len(result.ambiguous)}[/yellow]"
    )


def save_field_mappings(
    all_results: dict,
    output_base: str,
) -> dict:
    """
    将字段映射结果保存到 output/protocol/field_mappings/{form_name}.json

    Returns:
        dict[str, str]，form_name → 文件路径
    """
    mappings_dir = Path(output_base) / "field_mappings"
    mappings_dir.mkdir(parents=True, exist_ok=True)

    saved_paths = {}
    for form_name, result in all_results.items():
        fn = re.sub(r"[^a-z0-9]+", "_", form_name.lower()).strip("_") or "unknown_form"
        output_path = mappings_dir / f"{fn}.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        saved_paths[form_name] = str(output_path)

    console.print(f"[green]字段映射结果已保存到：{mappings_dir}[/green]")
    return saved_paths


def load_field_mapping(form_name: str, output_base: str) -> FieldMappingResult:
    """从文件读取指定 Form 的字段映射结果"""
    fn = re.sub(r"[^a-z0-9]+", "_", form_name.lower()).strip("_") or "unknown_form"
    mapping_path = Path(output_base) / "field_mappings" / f"{fn}.json"

    if not mapping_path.exists():
        return FieldMappingResult(form_name=form_name)

    with open(mapping_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    def _parse_fm(item: dict) -> FieldMapping:
        return FieldMapping(
            protocol_description=item.get("protocol_description", ""),
            matched_field_name=item.get("matched_field_name", ""),
            is_new=item.get("is_new", False),
            confidence=item.get("confidence", "medium"),
            reason=item.get("reason", ""),
            source_ref=item.get("source_ref", ""),
            source_text=item.get("source_text", ""),
        )

    return FieldMappingResult(
        form_name=data.get("form_name", form_name),
        mappings=[_parse_fm(m) for m in data.get("mappings", [])],
        matched=[_parse_fm(m) for m in data.get("matched", [])],
        unmatched=[_parse_fm(m) for m in data.get("unmatched", [])],
        ambiguous=[_parse_fm(m) for m in data.get("ambiguous", [])],
    )


def _strip_code_block(raw: str) -> str:
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw
