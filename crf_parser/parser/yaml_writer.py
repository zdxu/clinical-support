import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console

from .models import FieldDef, FormSpec

console = Console()


def _form_name_to_filename(form_name: str) -> str:
    """将 Form 名称转换为合法的文件名（小写，空格和特殊字符替换为下划线）"""
    name = form_name.lower()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    name = name.strip("_")
    return f"{name}.yaml"


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def write_single_crf_output(
    form_specs: list,
    crf_filename: str,
    output_base: str = "output/crf",
) -> str:
    """
    将单次解析结果写出到 output/crf/{crf_filename}/。
    返回输出目录路径。
    """
    output_dir = Path(output_base) / crf_filename
    output_dir.mkdir(parents=True, exist_ok=True)

    parsed_at = _now_iso()
    index_forms = []

    for spec in form_specs:
        filename = _form_name_to_filename(spec.form_name)
        file_path = output_dir / filename

        data = spec.to_dict()
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        index_forms.append({
            "form_name": spec.form_name,
            "file": filename,
            "field_count": len(spec.fields),
        })

    # 写出 _index.yaml
    # 取第一个 spec 的 project_name / version（应该都一样）
    project_name = form_specs[0].project_name if form_specs else ""
    version = form_specs[0].version if form_specs else ""

    index_data = {
        "crf_filename": crf_filename,
        "project_name": project_name,
        "version": version,
        "parsed_at": parsed_at,
        "total_forms": len(form_specs),
        "forms": index_forms,
    }
    with open(output_dir / "_index.yaml", "w", encoding="utf-8") as f:
        yaml.dump(index_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    return str(output_dir)


def merge_to_history(
    form_specs: list,
    output_base: str = "output/crf",
) -> None:
    """
    增量合并 form_specs 到 output/crf/history/。
    """
    history_dir = Path(output_base) / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    merged_at = _now_iso()

    for spec in form_specs:
        _merge_form_to_history(spec, history_dir, merged_at)

    _update_history_index(form_specs, history_dir, merged_at)


def _merge_form_to_history(spec: FormSpec, history_dir: Path, merged_at: str) -> None:
    """将单个 FormSpec 合并到 history 目录"""
    filename = _form_name_to_filename(spec.form_name)
    history_file = history_dir / filename

    source_entry = {
        "crf_filename": spec.source_crf,
        "version": spec.version,
        "merged_at": merged_at,
    }

    if not history_file.exists():
        # 情况A：直接写入
        data = spec.to_dict()
        data["history_sources"] = [source_entry]
        # 重排 key 顺序：form_name, history_sources, fields...
        ordered = {
            "form_name": data["form_name"],
            "history_sources": data["history_sources"],
        }
        for k, v in data.items():
            if k not in ordered:
                ordered[k] = v

        with open(history_file, "w", encoding="utf-8") as f:
            yaml.dump(ordered, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        console.print(f"  [green]history[/green] {spec.form_name} → 新增 ({filename})")
        return

    # 情况B：三方合并
    with open(history_file, "r", encoding="utf-8") as f:
        existing = yaml.safe_load(f)

    existing_fields_by_name = {
        fd["field_name"]: fd for fd in existing.get("fields", []) if fd.get("field_name")
    }
    new_fields_by_name = {
        fd.field_name: fd for fd in spec.fields if fd.field_name
    }

    merged_fields = []
    change_count = 0

    # 1. 新版本有的字段（覆盖或追加）
    for field_name, new_fd in new_fields_by_name.items():
        fd_dict = {
            "index": new_fd.index,
            "label": new_fd.label,
            "interaction": new_fd.interaction,
            "interaction_options": new_fd.interaction_options,
            "field_name": new_fd.field_name,
            "data_type": new_fd.data_type,
            "units": new_fd.units,
            "values": new_fd.values,
            "pre_filled_values": new_fd.pre_filled_values,
            "include_field_oid": new_fd.include_field_oid,
            "deprecated": False,
        }
        if field_name in existing_fields_by_name:
            old_fd = existing_fields_by_name[field_name]
            # 检查是否有变更
            changed = any(
                fd_dict.get(k) != old_fd.get(k)
                for k in ["label", "data_type", "interaction", "values"]
            )
            if changed:
                change_count += 1
        else:
            change_count += 1  # 新增字段
        merged_fields.append(fd_dict)

    # 2. 旧版本有、新版本没有的字段 → deprecated
    deprecated_count = 0
    for field_name, old_fd in existing_fields_by_name.items():
        if field_name not in new_fields_by_name:
            old_fd_copy = dict(old_fd)
            old_fd_copy["deprecated"] = True
            old_fd_copy["deprecated_since"] = spec.source_crf
            merged_fields.append(old_fd_copy)
            deprecated_count += 1

    # 按 index 排序
    merged_fields.sort(key=lambda x: x.get("index", 9999))

    # 更新 history_sources
    history_sources = existing.get("history_sources", [])
    history_sources.append(source_entry)

    # 构建最终数据
    final_data = {
        "form_name": spec.form_name,
        "history_sources": history_sources,
        "fields": merged_fields,
    }

    with open(history_file, "w", encoding="utf-8") as f:
        yaml.dump(final_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    summary = f"更新({change_count}字段变更"
    if deprecated_count:
        summary += f", {deprecated_count}字段deprecated"
    summary += ")"
    console.print(f"  [cyan]history[/cyan] {spec.form_name} → {summary}")


def _update_history_index(form_specs: list, history_dir: Path, merged_at: str) -> None:
    """更新 _history_index.yaml"""
    index_file = history_dir / "_history_index.yaml"

    if index_file.exists():
        with open(index_file, "r", encoding="utf-8") as f:
            existing_index = yaml.safe_load(f) or {}
    else:
        existing_index = {}

    crf_sources = existing_index.get("crf_sources", [])
    if form_specs:
        new_source = form_specs[0].source_crf
        if new_source not in crf_sources:
            crf_sources.append(new_source)

    # 重建 forms 列表（扫描 history 目录所有 yaml 文件）
    forms_list = []
    for yaml_file in sorted(history_dir.glob("*.yaml")):
        if yaml_file.name.startswith("_"):
            continue
        with open(yaml_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        sources = data.get("history_sources", [])
        last_source = sources[-1]["crf_filename"] if sources else ""
        forms_list.append({
            "form_name": data.get("form_name", yaml_file.stem),
            "file": yaml_file.name,
            "field_count": len([fd for fd in data.get("fields", []) if not fd.get("deprecated", False)]),
            "last_updated_from": last_source,
        })

    index_data = {
        "last_updated": merged_at,
        "total_forms": len(forms_list),
        "crf_sources": crf_sources,
        "forms": forms_list,
    }

    with open(index_file, "w", encoding="utf-8") as f:
        yaml.dump(index_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
