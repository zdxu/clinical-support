"""
映射模块 A：Form 级 LLM 语义映射

将 Protocol 提取的 Form 名称映射到 history/ 模板库文件。
支持一对多映射（如 "Laboratory Tests" → 多个 Lab Form 文件）。
"""

import json
import time
from glob import glob
from os.path import basename
from pathlib import Path

import yaml
from rich.console import Console

from .models import FormMapping, FormMappingResult

console = Console()


def build_template_summary(history_dir: str) -> list:
    """
    读取 history/ 下所有 YAML，生成供 LLM 参考的模板摘要列表。
    每个模板只提取关键信息，不传入完整字段定义（节省 token）。

    Returns:
        list[dict]，每个 dict 含 file、form_name、sample_labels
    """
    summaries = []
    history_path = Path(history_dir)
    if not history_path.exists():
        return summaries

    for yaml_file in sorted(glob(f"{history_dir}/*.yaml")):
        fname = basename(yaml_file)
        # 跳过索引文件
        if fname.startswith("_") or fname.endswith("_index.yaml"):
            continue
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            summaries.append({
                "file": fname,
                "form_name": data.get("form_name", Path(yaml_file).stem),
                "sample_labels": [
                    field.get("label", "")
                    for field in data.get("fields", [])[:5]
                    if field.get("label")
                ],
            })
        except Exception as e:
            console.print(f"[yellow]警告：读取模板 {fname} 失败：{e}[/yellow]")

    return summaries


def map_forms(
    protocol_forms: list,
    template_summaries: list,
    client,
) -> FormMappingResult:
    """
    使用 LLM 将 Protocol Form 名称语义映射到模板库文件。

    protocol_forms: Phase 1 提取的 Form 名称列表
    template_summaries: build_template_summary() 的输出
    client: OpenAI client

    Returns:
        FormMappingResult
    """
    if not protocol_forms:
        return FormMappingResult()

    protocol_forms_json = json.dumps(protocol_forms, ensure_ascii=False, indent=2)
    template_summaries_json = json.dumps(template_summaries, ensure_ascii=False, indent=2)

    system_prompt = (
        "你是临床试验数据管理专家。\n"
        "任务：将 Protocol 中的评估项目名称映射到模板库中对应的 Form。\n\n"
        "映射规则：\n"
        "1. 名称完全一致（不区分大小写） → confidence=high\n"
        "2. 明确的缩写/全称关系（如 ECG/Electrocardiogram） → confidence=high\n"
        "3. 业务别名（如 CBC=Complete Blood Count=Hematology） → confidence=high\n"
        "4. 包含关系（如 Laboratory Tests 包含 Hematology+Chemistry） → confidence=medium，"
        "matched_templates 填多个\n"
        "5. 语义相似但不确定 → confidence=low\n"
        "6. 模板库中明确没有对应 → is_new=true，matched_templates=[]，confidence=high\n\n"
        "只输出 JSON，不要有其他文字。"
    )

    user_prompt = (
        f"【Protocol 评估项目列表】\n"
        f"{protocol_forms_json}\n\n"
        f"【模板库 Form 清单（含样本字段作为语义参考）】\n"
        f"{template_summaries_json}\n\n"
        "请为每个 Protocol 评估项目输出映射结果，格式：\n"
        '[\n'
        '  {\n'
        '    "protocol_form": "Hematology",\n'
        '    "matched_templates": ["lbhema.yaml"],\n'
        '    "is_new": false,\n'
        '    "confidence": "high",\n'
        '    "reason": "名称完全一致"\n'
        '  },\n'
        '  {\n'
        '    "protocol_form": "Laboratory Tests",\n'
        '    "matched_templates": ["lbhema.yaml", "lbchem.yaml"],\n'
        '    "is_new": false,\n'
        '    "confidence": "medium",\n'
        '    "reason": "Laboratory Tests 通常包含血常规、生化等多个 Lab Form"\n'
        '  },\n'
        '  {\n'
        '    "protocol_form": "Tumor Response",\n'
        '    "matched_templates": [],\n'
        '    "is_new": true,\n'
        '    "confidence": "high",\n'
        '    "reason": "模板库中无对应 Form，需根据 Protocol 描述新建"\n'
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

            mappings = []
            for item in items:
                mappings.append(FormMapping(
                    protocol_form=item.get("protocol_form", ""),
                    matched_templates=item.get("matched_templates", []),
                    is_new=item.get("is_new", False),
                    confidence=item.get("confidence", "medium"),
                    reason=item.get("reason", ""),
                ))

            # 处理 LLM 可能漏掉的 Form
            mapped_forms = {m.protocol_form for m in mappings}
            for pf in protocol_forms:
                if pf not in mapped_forms:
                    mappings.append(FormMapping(
                        protocol_form=pf,
                        matched_templates=[],
                        is_new=True,
                        confidence="low",
                        reason="LLM 未返回此 Form 的映射结果",
                    ))

            result = _group_mappings(mappings)
            _print_mapping_result(result)
            return result

        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                console.print(f"[red]Form 级映射失败：{e}[/red]")
                # 降级：所有 Form 标记为 low 置信度
                mappings = [
                    FormMapping(
                        protocol_form=pf,
                        matched_templates=[],
                        is_new=True,
                        confidence="low",
                        reason=f"LLM 调用失败：{e}",
                    )
                    for pf in protocol_forms
                ]
                result = _group_mappings(mappings)
                return result

    return FormMappingResult()


def _group_mappings(mappings: list) -> FormMappingResult:
    """按置信度和是否新建分组"""
    result = FormMappingResult(mappings=mappings)

    for m in mappings:
        if m.is_new:
            result.new_forms.append(m)
        elif m.confidence == "high":
            result.high.append(m)
        elif m.confidence == "medium":
            result.medium.append(m)
        else:
            result.low.append(m)

    return result


def _print_mapping_result(result: FormMappingResult):
    """用 rich 打印映射结果"""
    console.print("\n[bold cyan]── Form 级映射结果 ──[/bold cyan]")

    for m in result.high:
        templates_str = ", ".join(m.matched_templates) if m.matched_templates else "（无模板）"
        console.print(f"  [green]✓ high  [/green] {m.protocol_form:<30} → {templates_str}")

    for m in result.medium:
        templates_str = ", ".join(m.matched_templates) if m.matched_templates else "（无模板）"
        console.print(f"  [yellow]⚠ medium[/yellow] {m.protocol_form:<30} → {templates_str}（建议确认）")

    for m in result.low:
        templates_str = ", ".join(m.matched_templates) if m.matched_templates else "（无模板）"
        console.print(f"  [red]⚠ low   [/red] {m.protocol_form:<30} → {templates_str}（需人工确认）")

    for m in result.new_forms:
        console.print(f"  [blue]+ new   [/blue] {m.protocol_form:<30} → 无匹配，需新建")

    console.print(
        f"\n  ────────────────────────────────────────\n"
        f"  high:{len(result.high)}  medium:{len(result.medium)}  "
        f"low:{len(result.low)}  新建:{len(result.new_forms)}"
    )


def save_form_mapping(result: FormMappingResult, output_base: str) -> str:
    """将 Form 映射结果保存到 output/protocol/form_mapping.json"""
    output_path = Path(output_base) / "form_mapping.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
    console.print(f"[green]Form 映射结果已保存：{output_path}[/green]")
    return str(output_path)


def load_form_mapping(output_base: str) -> FormMappingResult:
    """从 output/protocol/form_mapping.json 读取已保存的映射结果"""
    mapping_path = Path(output_base) / "form_mapping.json"
    if not mapping_path.exists():
        return FormMappingResult()

    with open(mapping_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    def _parse_mapping(item: dict) -> FormMapping:
        return FormMapping(
            protocol_form=item.get("protocol_form", ""),
            matched_templates=item.get("matched_templates", []),
            is_new=item.get("is_new", False),
            confidence=item.get("confidence", "medium"),
            reason=item.get("reason", ""),
        )

    result = FormMappingResult(
        mappings=[_parse_mapping(m) for m in data.get("mappings", [])],
        high=[_parse_mapping(m) for m in data.get("high", [])],
        medium=[_parse_mapping(m) for m in data.get("medium", [])],
        low=[_parse_mapping(m) for m in data.get("low", [])],
        new_forms=[_parse_mapping(m) for m in data.get("new_forms", [])],
    )
    return result


def _strip_code_block(raw: str) -> str:
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw
