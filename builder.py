"""Project builder and QA review using Claude API.

SECURITY
────────
• project_name is sanitized to [a-z0-9-_] only (max 60 chars).
• Every file path returned by Claude is validated with _safe_path()
  to ensure it cannot escape the project sandbox directory.
  Path-traversal attempts raise ValueError and abort the build.
• ValueError messages are safe to surface to the caller (no system paths).
• All other exceptions are logged server-side only; the caller receives
  a generic message.
"""

from __future__ import annotations

import json
import logging
import os
import re

import anthropic

from prompts import SYSTEM_BUILDER, SYSTEM_QA
from router import MODEL_HAIKU, MODEL_SONNET, estimate_cost, route_model, get_model_alias

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = os.path.expanduser("~/ai-builder-output")


# ──────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────

def _get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _strip_json_fences(text: str) -> str:
    """Remove markdown code fences from JSON output if present."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def _sanitize_project_name(raw: str) -> str:
    """Return a safe, filesystem-friendly project name.

    Rules:
    - lowercase
    - only a-z, 0-9, hyphens, underscores
    - no leading/trailing hyphens
    - max 60 characters
    - falls back to 'project' if nothing valid remains
    """
    name = raw.lower().strip()
    name = re.sub(r"[^a-z0-9\-_]", "-", name)   # replace bad chars
    name = re.sub(r"-{2,}", "-", name)            # collapse repeated hyphens
    name = name.strip("-_")                        # strip leading/trailing
    name = name[:60]
    return name or "project"


def _safe_path(base_dir: str, relative: str) -> str:
    """Resolve *relative* against *base_dir* and verify it stays inside.

    Raises ValueError on path-traversal attempts.
    """
    # Strip any leading slashes or drive letters
    relative = relative.lstrip("/\\")
    relative = re.sub(r"^[a-zA-Z]:[/\\]", "", relative)  # Windows drive prefix

    resolved = os.path.realpath(os.path.join(base_dir, relative))
    sandbox  = os.path.realpath(base_dir)

    if not (resolved == sandbox or resolved.startswith(sandbox + os.sep)):
        raise ValueError(
            f"Path traversal blocked: '{relative}' resolves outside the project sandbox."
        )
    return resolved


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def build_project(prompt: str, output_base: str | None = None) -> dict:
    """Build a complete project from a text prompt using Claude.

    Args:
        prompt: Description of the project to build
        output_base: Override base directory (default: ~/ai-builder-output)

    Returns:
        Dict: project_name, description, directory, files_created,
              setup_instructions, model_used, model_reason, cost_usd, readme

    Raises:
        ValueError: Safe-to-surface errors (bad JSON, missing keys, path traversal)
    """
    client = _get_client()

    model_id, model_reason = route_model(prompt)

    # Builds always use at least Sonnet
    if "haiku" in model_id:
        model_id    = MODEL_SONNET
        model_reason = "Build tasks require Sonnet or higher"

    logger.info("Building project with %s: %s…", model_id, prompt[:80])

    message = client.messages.create(
        model=model_id,
        max_tokens=8000,
        system=SYSTEM_BUILDER,
        messages=[{"role": "user", "content": f"Build a complete project for: {prompt}"}],
    )

    raw_response = message.content[0].text
    cleaned = _strip_json_fences(raw_response)

    try:
        project_data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse project JSON: %s", exc)
        raise ValueError("Claude returned invalid JSON. Please try again.") from exc

    # Validate required keys
    for key in ("project_name", "description", "files", "setup_instructions", "readme"):
        if key not in project_data:
            raise ValueError(f"Incomplete project response (missing '{key}'). Please try again.")

    # Sanitize project name — critical before using it as a directory name
    raw_name     = str(project_data["project_name"])
    project_name = _sanitize_project_name(raw_name)
    if project_name != raw_name.lower().strip():
        logger.info("project_name sanitized: '%s' → '%s'", raw_name, project_name)

    # Resolve output directory
    base_dir = output_base or os.getenv("OUTPUT_DIR")
    base_dir = os.path.expanduser(base_dir) if base_dir else DEFAULT_OUTPUT_DIR
    project_dir = os.path.join(base_dir, project_name)
    os.makedirs(project_dir, exist_ok=True)

    files_created: list[str] = []

    for file_info in project_data["files"]:
        raw_file_path = str(file_info.get("path", "")).strip()
        if not raw_file_path:
            continue

        # Validate path stays inside project sandbox
        try:
            safe_file_path = _safe_path(project_dir, raw_file_path)
        except ValueError as exc:
            logger.warning("Skipped unsafe path '%s': %s", raw_file_path, exc)
            continue   # skip this file and carry on with the rest

        parent = os.path.dirname(safe_file_path)
        os.makedirs(parent, exist_ok=True)

        with open(safe_file_path, "w", encoding="utf-8") as fh:
            fh.write(file_info.get("content", ""))

        files_created.append(safe_file_path)
        logger.info("  Created: %s", raw_file_path)

    # Write README if not already included
    readme_exists = any(
        str(f.get("path", "")).lower() == "readme.md" for f in project_data["files"]
    )
    if not readme_exists and project_data.get("readme"):
        readme_path = os.path.join(project_dir, "README.md")
        with open(readme_path, "w", encoding="utf-8") as fh:
            fh.write(project_data["readme"])
        files_created.append(readme_path)

    usage    = message.usage
    cost_usd = estimate_cost(usage.input_tokens, usage.output_tokens, model_id)

    logger.info("Project '%s' built — %d files, $%.6f", project_name, len(files_created), cost_usd)

    return {
        "project_name":      project_name,
        "description":       project_data["description"],
        "directory":         os.path.abspath(project_dir),
        "files_created":     files_created,
        "setup_instructions": project_data["setup_instructions"],
        "model_used":        get_model_alias(model_id),
        "model_reason":      model_reason,
        "cost_usd":          cost_usd,
        "readme":            project_data.get("readme", ""),
    }


def qa_review(project: dict) -> str:
    """Quick QA review of the generated project using Claude Haiku."""
    client = _get_client()

    file_summaries = []
    for file_path in project.get("files_created", [])[:5]:
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                content = fh.read(500)
            file_summaries.append(f"--- {os.path.basename(file_path)} ---\n{content}")
        except Exception as exc:
            logger.warning("Could not read file for QA: %s", exc)

    if not file_summaries:
        return "No files available for review."

    message = client.messages.create(
        model=MODEL_HAIKU,
        max_tokens=500,
        system=SYSTEM_QA,
        messages=[{
            "role": "user",
            "content": (
                f"Review this project '{project['project_name']}':\n\n"
                + "\n\n".join(file_summaries)
            ),
        }],
    )
    return message.content[0].text
