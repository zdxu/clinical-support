"""
CRF YAML 输出验证脚本

用法：
  # 验证单次输出
  python validate_output.py --target output/crf/ADG138-1001-CRF-draft-V1/

  # 验证合并模板库
  python validate_output.py --target output/crf/history/
"""

import argparse
import re
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.table import Table

console = Console()

# 合法的 Data Type 模式
VALID_DATA_TYPES = [
    re.compile(r"^\$\d+$"),                    # $1, $200 等
    re.compile(r"^dd MMM yyyy$"),
    re.compile(r"^dd MMM yyyy hh:mm$"),
    re.compile(r"^\d+$"),                       # 纯数字
    re.compile(r"^numeric$", re.IGNORECASE),
    re.compile(r"^integer$", re.IGNORECASE),
    re.compile(r"^float$", re.IGNORECASE),
]


def is_valid_data_type(dt: str) -> bool:
    if not dt:
        return True  # 空字符串不触发警告
    return any(p.match(dt) for p in VALID_DATA_TYPES)


def validate_form_yaml(yaml_file: Path, is_history: bool) -> dict:
    """
    验证单个 Form YAML 文件。

    Returns:
        dict with keys: form_name, errors, warnings, checks_total
    """
    with open(yaml_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    form_name = data.get("form_name", yaml_file.stem)
    errors = []
    warnings = []

    fields = data.get("fields", [])

    # 1. 字段完整性
    if not fields:
        errors.append("fields 列表为空")
        return {
            "form_name": form_name,
            "errors": errors,
            "warnings": warnings,
            "checks_total": 1,
        }

    # 2. 序号连续性：index 从1开始，无跳号
    indices = [f.get("index") for f in fields if not f.get("deprecated", False)]
    indices = [i for i in indices if i is not None]
    if indices:
        indices_sorted = sorted(indices)
        expected = list(range(1, len(indices_sorted) + 1))
        if indices_sorted != expected:
            missing = set(expected) - set(indices_sorted)
            extra = set(indices_sorted) - set(expected)
            msg = f"序号不连续（期望 1-{len(expected)}"
            if missing:
                msg += f"，缺少: {sorted(missing)}"
            if extra:
                msg += f"，多余: {sorted(extra)}"
            msg += "）"
            errors.append(msg)

    # 3. 关键字段非空
    for fd in fields:
        if fd.get("deprecated", False):
            continue
        idx = fd.get("index", "?")
        if not fd.get("field_name", "").strip():
            errors.append(f"index={idx}: field_name 为空")
        if not fd.get("include_field_oid", "").strip():
            errors.append(f"index={idx}: include_field_oid 为空")

    # 4. Data Type 合法性
    for fd in fields:
        if fd.get("deprecated", False):
            continue
        dt = fd.get("data_type", "")
        if dt and not is_valid_data_type(dt):
            warnings.append(f"index={fd.get('index','?')} ({fd.get('field_name','')}): data_type='{dt}' 不在标准列表（可能是合法自定义值）")

    # 5. include_field_oid 与 field_name 不一致
    for fd in fields:
        if fd.get("deprecated", False):
            continue
        fn = fd.get("field_name", "")
        oid = fd.get("include_field_oid", "")
        if fn and oid and fn != oid:
            warnings.append(f"index={fd.get('index','?')}: include_field_oid='{oid}' ≠ field_name='{fn}'（可能引用共享字段库）")

    # 6. interaction = checkbox 但 interaction_options 为空
    for fd in fields:
        if fd.get("deprecated", False):
            continue
        if fd.get("interaction") == "checkbox" and not fd.get("interaction_options"):
            errors.append(f"index={fd.get('index','?')} ({fd.get('field_name','')}): interaction=checkbox 但 interaction_options 为空")

    # History 专项检查
    if is_history:
        # 7. deprecated 字段占比 > 30%
        deprecated_count = sum(1 for f in fields if f.get("deprecated", False))
        total_count = len(fields)
        if total_count > 0 and deprecated_count / total_count > 0.3:
            pct = deprecated_count / total_count * 100
            warnings.append(f"deprecated 字段占比 {pct:.1f}% > 30%（模板可能严重过时）")

    checks_total = 6 + (1 if is_history else 0)
    return {
        "form_name": form_name,
        "errors": errors,
        "warnings": warnings,
        "checks_total": checks_total,
        "field_count": len([f for f in fields if not f.get("deprecated", False)]),
    }


def validate_history_index_consistency(target_dir: Path) -> list:
    """检查 _history_index 的 crf_sources 与各文件 history_sources 是否一致"""
    errors = []
    index_file = target_dir / "_history_index.yaml"
    if not index_file.exists():
        errors.append("_history_index.yaml 不存在")
        return errors

    with open(index_file, "r", encoding="utf-8") as f:
        index_data = yaml.safe_load(f) or {}

    index_sources = set(index_data.get("crf_sources", []))

    for yaml_file in sorted(target_dir.glob("*.yaml")):
        if yaml_file.name.startswith("_"):
            continue
        with open(yaml_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        file_sources = set(
            s["crf_filename"] for s in data.get("history_sources", [])
        )
        extra_in_file = file_sources - index_sources
        if extra_in_file:
            errors.append(
                f"{yaml_file.name}: history_sources 包含 index 中没有的来源: {extra_in_file}"
            )

    return errors


def main():
    argp = argparse.ArgumentParser(description="验证 CRF YAML 输出质量")
    argp.add_argument("--target", required=True, help="验证目标目录")
    args = argp.parse_args()

    target_dir = Path(args.target)
    if not target_dir.exists():
        console.print(f"[red]目录不存在: {target_dir}[/red]")
        sys.exit(1)

    is_history = "history" in target_dir.parts or target_dir.name == "history"
    console.print(f"\n[bold]验证目标:[/bold] {target_dir}")
    console.print(f"[bold]模式:[/bold] {'history 模板库' if is_history else '单次输出'}\n")

    yaml_files = [f for f in sorted(target_dir.glob("*.yaml")) if not f.name.startswith("_")]

    if not yaml_files:
        console.print("[yellow]未找到 YAML 文件[/yellow]")
        sys.exit(0)

    total_checks = 0
    total_pass = 0
    total_warnings = 0
    total_errors = 0

    form_results = []

    for yaml_file in yaml_files:
        result = validate_form_yaml(yaml_file, is_history)
        form_results.append(result)

        console.print(f"[bold cyan][{result['form_name']}][/bold cyan]  ({result['field_count']} 个有效字段)")

        # 序号连续性
        if not any("序号不连续" in e for e in result["errors"]):
            console.print(f"  [green]✓[/green] 序号连续性")
            total_pass += 1
        else:
            for e in result["errors"]:
                if "序号" in e:
                    console.print(f"  [red]✗[/red] 序号连续性: {e}")
                    total_errors += 1
        total_checks += 1

        # 字段完整性
        if not any("fields 列表" in e for e in result["errors"]):
            console.print(f"  [green]✓[/green] 字段完整性: {result['field_count']} 个字段")
            total_pass += 1
        else:
            console.print(f"  [red]✗[/red] 字段完整性: fields 列表为空")
            total_errors += 1
        total_checks += 1

        # 关键字段非空
        key_errors = [e for e in result["errors"] if "field_name" in e or "include_field_oid" in e]
        if not key_errors:
            console.print(f"  [green]✓[/green] 关键字段非空")
            total_pass += 1
        else:
            for e in key_errors:
                console.print(f"  [red]✗[/red] 关键字段非空: {e}")
                total_errors += 1
        total_checks += 1

        # Data Type 警告
        dt_warnings = [w for w in result["warnings"] if "data_type" in w]
        if not dt_warnings:
            console.print(f"  [green]✓[/green] Data Type 格式")
            total_pass += 1
        else:
            for w in dt_warnings:
                console.print(f"  [yellow]⚠[/yellow] Data Type 警告: {w}")
                total_warnings += 1
        total_checks += 1

        # include_field_oid 不一致警告
        oid_warnings = [w for w in result["warnings"] if "include_field_oid" in w]
        for w in oid_warnings:
            console.print(f"  [yellow]⚠[/yellow] {w}")
            total_warnings += 1

        # interaction 类型
        cb_errors = [e for e in result["errors"] if "checkbox" in e]
        if not cb_errors:
            console.print(f"  [green]✓[/green] interaction 类型正确")
            total_pass += 1
        else:
            for e in cb_errors:
                console.print(f"  [red]✗[/red] {e}")
                total_errors += 1
        total_checks += 1

        # deprecated 占比（history 专项）
        if is_history:
            dep_warnings = [w for w in result["warnings"] if "deprecated" in w]
            if not dep_warnings:
                console.print(f"  [green]✓[/green] deprecated 字段占比正常")
                total_pass += 1
            else:
                for w in dep_warnings:
                    console.print(f"  [yellow]⚠[/yellow] {w}")
                    total_warnings += 1
            total_checks += 1

        console.print()

    # History index 一致性检查（history 专项）
    if is_history:
        console.print("[bold cyan][_history_index 一致性][/bold cyan]")
        index_errors = validate_history_index_consistency(target_dir)
        if not index_errors:
            console.print(f"  [green]✓[/green] crf_sources 与各文件 history_sources 一致")
            total_pass += 1
        else:
            for e in index_errors:
                console.print(f"  [red]✗[/red] {e}")
                total_errors += 1
        total_checks += 1
        console.print()

    # 最终汇总
    pass_rate = total_pass / total_checks * 100 if total_checks else 0

    console.print("[bold]── 最终汇总 ──[/bold]")
    console.print(
        f"总检查数：[bold]{total_checks}[/bold] | "
        f"通过：[green]{total_pass}[/green] | "
        f"警告：[yellow]{total_warnings}[/yellow] | "
        f"错误：[red]{total_errors}[/red]"
    )
    color = "green" if total_errors == 0 else "red"
    console.print(f"质量得分：[{color}]{pass_rate:.1f}%[/{color}]")

    sys.exit(1 if total_errors > 0 else 0)


if __name__ == "__main__":
    main()
