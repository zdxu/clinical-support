"""
模块5：归并 + 模板对比

Step A：字段归属归并   assign_findings_to_forms()
Step B：模板对比       compare_with_template()
Step C：新建Form草稿   handle_unmatched_forms()

全部使用纯文本 LLM 调用（无图片）。
"""

import json
import time
from pathlib import Path

import yaml
from rich.console import Console

from .models import FieldFinding, FieldChange

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
# Step B：模板对比
# ─────────────────────────────────────────────

def compare_with_template(
    form_name: str,
    form_findings: list,
    template_fields: list,
    client,
) -> list:
    """
    对比 Protocol 字段描述和模板字段，输出 FieldChange 列表。

    template_fields: list of dict（从 history YAML 读取的字段列表）
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
        [f.to_dict() for f in form_findings],
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

            changes = []
            for item in data.get("changes", []):
                changes.append(FieldChange(
                    change_type=item.get("change_type", ""),
                    field_name=item.get("field_name", ""),
                    detail=item.get("detail", {}),
                    reason=item.get("reason", ""),
                    confidence=item.get("confidence", "medium"),
                    source_ref=item.get("source_ref", ""),
                    source_text=item.get("source_text", ""),
                ))
            return changes

        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                console.print(f"[red]模板对比失败 [{form_name}]：{e}[/red]")
                return []


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
