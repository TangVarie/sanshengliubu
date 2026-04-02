"""Export utilities — convert prompt system to Markdown / JSON for download."""

from __future__ import annotations

import json
from typing import Any


def export_as_markdown(prompt_system: dict[str, Any], project_name: str = "") -> str:
    """Convert a prompt system dict to formatted Markdown."""
    lines: list[str] = []
    lines.append(f"# {project_name or 'Prompt System'}")
    lines.append("")

    # Usage guide
    guide = prompt_system.get("usage_guide", "")
    if guide:
        lines.append("## 使用指南")
        lines.append("")
        lines.append(guide)
        lines.append("")

    # Prompt templates
    templates = prompt_system.get("prompt_templates", [])
    if templates:
        lines.append("## Prompt 模板")
        lines.append("")

        for i, t in enumerate(templates, 1):
            d_id = t.get("direction_id", f"D{i}")
            d_name = t.get("direction_name", f"方向 {i}")
            lines.append(f"### {d_id}: {d_name}")
            lines.append("")

            sp = t.get("system_prompt", "")
            if sp:
                lines.append("#### System Prompt")
                lines.append("")
                lines.append("```")
                lines.append(sp)
                lines.append("```")
                lines.append("")

            up = t.get("user_prompt_template", "")
            if up:
                lines.append("#### User Prompt Template")
                lines.append("")
                lines.append("```")
                lines.append(up)
                lines.append("```")
                lines.append("")

            variables = t.get("variables", {})
            if variables:
                lines.append("#### 变量说明")
                lines.append("")
                for var_name, var_desc in variables.items():
                    lines.append(f"- `{{{{{var_name}}}}}`: {var_desc}")
                lines.append("")

            demo = t.get("demo_output", "")
            if demo:
                lines.append("#### 示例输出")
                lines.append("")
                lines.append(demo)
                lines.append("")

            lines.append("---")
            lines.append("")

    # Uncertainty summary
    uncertainty_summary = prompt_system.get("_uncertainty_summary", {})
    if uncertainty_summary:
        lines.append("## ⚠️ 数据验证清单")
        lines.append("")
        lines.append("> 以下内容基于 AI 推断，建议在使用前用真实数据验证")
        lines.append("")

        for item in uncertainty_summary.get("high_impact", []):
            lines.append(f"- 🔴 **{item.get('source', '')}** / {item.get('field', '')}: {item.get('reason', '')}")
            lines.append(f"  - 建议补充: {item.get('data_suggestion', '')}")
        for item in uncertainty_summary.get("medium_impact", []):
            lines.append(f"- 🟡 **{item.get('source', '')}** / {item.get('field', '')}: {item.get('reason', '')}")
            lines.append(f"  - 建议补充: {item.get('data_suggestion', '')}")

        checklist = uncertainty_summary.get("data_checklist", [])
        if checklist:
            lines.append("")
            lines.append("### 建议补充的数据源")
            lines.append("")
            for c in checklist:
                lines.append(f"- [ ] {c}")
        lines.append("")

    # Batch rules
    batch_rules = prompt_system.get("batch_rules", {})
    if batch_rules:
        lines.append("## 批量管理规则")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(batch_rules, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")

    # Demo outputs
    demos = prompt_system.get("demo_outputs", [])
    if demos:
        lines.append("## 完整示例")
        lines.append("")
        for demo in demos:
            d_id = demo.get("direction_id", "")
            platform = demo.get("platform", "")
            persona = demo.get("persona_used", "")
            lines.append(f"### {d_id} — {platform} (人设: {persona})")
            lines.append("")
            lines.append(demo.get("output_content", ""))
            lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines)


def export_as_json(prompt_system: dict[str, Any]) -> str:
    """Pretty-print JSON export of prompt system."""
    return json.dumps(prompt_system, ensure_ascii=False, indent=2)
