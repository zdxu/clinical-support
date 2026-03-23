import base64
import json
import time
from pathlib import Path

import anthropic
from rich.console import Console

from .models import FieldDef, FormPages

console = Console()

VIEW_SYSTEM_PROMPT = """你是临床试验 CRF 文档解析专家。
我给你的图片是一个 Form 的视图块页面（可能多页）。
页面包含两列表格：左列是 Label 文字，右列是带圆圈序号（①②③ 或 (1)(2)(3)）和可能的交互选项（Yes/No）。

请提取每行的 index（序号数字）、label、interaction 和 interaction_options。

interaction 判断规则：
- 视图块右侧有 "Yes" 和 "No" 两行 → interaction = "checkbox"，interaction_options = ["Yes", "No"]
- 其他情况 → interaction = "text"，interaction_options = []

只输出 JSON，不要有任何其他文字。

输出格式：
{"view_fields": [{"index": 1, "label": "Was this visit performed?", "interaction": "checkbox", "interaction_options": ["Yes", "No"]}]}"""

META_SYSTEM_PROMPT = """你是临床试验 CRF 文档解析专家。
我给你的图片是一个 Form 的元定义块页面（可能多页）。
页面包含六列表格，列标题为：Field Name | Data Type | Units | Values | Pre-Filled Values | Include Field OID
每行前有带圆圈序号（①②③ 或 (1)(2)(3)），请提取每行的全部字段。

interaction 判断（写入 values 后自动推断）：
- 如果 Values 列包含多个选项且不止 Y=Yes/N=No 格式 → interaction = "dropdown"
- 其他情况 → 保留 view 块判断结果

只输出 JSON，不要有任何其他文字。

输出格式：
{"meta_fields": [{"index": 1, "field_name": "VISYN", "data_type": "$1", "units": "", "values": "Y=Yes\\nN=No", "pre_filled_values": "", "include_field_oid": "VISYN"}]}"""


def _encode_image(image_path: str) -> str:
    """将图片文件编码为 base64 字符串"""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _build_image_content(image_paths: list) -> list:
    """构建多图片的 content 列表"""
    content = []
    for path in image_paths:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": _encode_image(path),
            },
        })
    return content


def _call_llm_with_retry(
    client: anthropic.Anthropic,
    system_prompt: str,
    image_paths: list,
    user_text: str,
    form_name: str,
    call_type: str,
    tmp_dir: str = "tmp",
    max_retries: int = 3,
) -> dict:
    """调用 LLM Vision，含 retry 逻辑和错误保存"""
    content = _build_image_content(image_paths)
    content.append({"type": "text", "text": user_text})

    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            )

            raw = response.content[0].text.strip()

            # 去掉 ```json ``` 标记
            if raw.startswith("```"):
                parts = raw.split("```")
                raw = parts[1] if len(parts) > 1 else parts[0]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            return json.loads(raw)

        except json.JSONDecodeError as e:
            error_path = Path(tmp_dir) / f"{form_name.lower().replace(' ', '_')}_{call_type}_error_response.txt"
            error_path.parent.mkdir(parents=True, exist_ok=True)
            error_path.write_text(response.content[0].text if "response" in dir() else str(e))
            console.print(f"[red]JSON 解析失败，原始响应已保存至 {error_path}[/red]")
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise


def parse_form_with_vision(
    form_pages: FormPages,
    client: anthropic.Anthropic,
    tmp_dir: str = "tmp",
) -> list:
    """
    使用 LLM Vision 解析一个 FormPages，返回 FieldDef 列表。

    分两次调用：
    1. view_pages → 提取 label 和 interaction
    2. field_meta_pages → 提取字段元定义
    最后用 index 合并为完整 FieldDef 列表
    """
    form_name = form_pages.form_name

    # ── 第1次调用：view 块 ──
    view_image_paths = [p.image_path for p in form_pages.view_pages]
    if not view_image_paths:
        console.print(f"[yellow]WARNING: {form_name} 没有 view 页面，跳过[/yellow]")
        return []

    view_result = _call_llm_with_retry(
        client=client,
        system_prompt=VIEW_SYSTEM_PROMPT,
        image_paths=view_image_paths,
        user_text="请提取该 Form 视图块的所有字段信息，返回 JSON。",
        form_name=form_name,
        call_type="view",
        tmp_dir=tmp_dir,
    )
    view_fields = {f["index"]: f for f in view_result.get("view_fields", [])}

    # ── 第2次调用：field_meta 块 ──
    meta_image_paths = [p.image_path for p in form_pages.field_meta_pages]
    if not meta_image_paths:
        console.print(f"[yellow]WARNING: {form_name} 没有 field_meta 页面，跳过[/yellow]")
        return []

    meta_result = _call_llm_with_retry(
        client=client,
        system_prompt=META_SYSTEM_PROMPT,
        image_paths=meta_image_paths,
        user_text="请提取该 Form 元定义块的所有字段信息，返回 JSON。",
        form_name=form_name,
        call_type="meta",
        tmp_dir=tmp_dir,
    )
    meta_fields = {f["index"]: f for f in meta_result.get("meta_fields", [])}

    # ── 合并：以 view_fields 的 index 为主键 ──
    all_indices = sorted(set(list(view_fields.keys()) + list(meta_fields.keys())))
    field_defs = []

    for idx in all_indices:
        vf = view_fields.get(idx)
        mf = meta_fields.get(idx)

        if vf is None:
            console.print(f"[yellow]WARNING: {form_name} index={idx} 在 view 块缺失，跳过[/yellow]")
            continue
        if mf is None:
            console.print(f"[yellow]WARNING: {form_name} index={idx} 在 field_meta 块缺失，跳过[/yellow]")
            continue

        # 推断 interaction：若 Values 列有多个非 Y/N 选项 → dropdown
        values_str = mf.get("values", "")
        interaction = vf.get("interaction", "text")
        interaction_options = vf.get("interaction_options", [])

        if interaction != "checkbox" and values_str:
            # Values 列存在且不是简单的 Yes/No checkbox → dropdown
            lines = [v.strip() for v in values_str.replace(";", "\n").split("\n") if v.strip()]
            if len(lines) > 1:
                interaction = "dropdown"
                interaction_options = lines

        field_defs.append(FieldDef(
            index=idx,
            label=vf.get("label", ""),
            interaction=interaction,
            interaction_options=interaction_options,
            field_name=mf.get("field_name", ""),
            data_type=mf.get("data_type", ""),
            units=mf.get("units", ""),
            values=values_str,
            pre_filled_values=mf.get("pre_filled_values", ""),
            include_field_oid=mf.get("include_field_oid", ""),
            deprecated=False,
        ))

    return field_defs
