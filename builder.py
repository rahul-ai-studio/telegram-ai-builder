"""Project builder and QA review using Claude API."""

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


def _get_client() -> anthropic.Anthropic:
    """Get an Anthropic client instance."""
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def _strip_json_fences(text: str) -> str:
    """Remove markdown code fences from JSON output."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def build_project(prompt: str, output_base: str | None = None) -> dict:
    """Build a complete project from a text prompt using Claude.

    Args:
        prompt: Description of the project to build
        output_base: Base directory for output (default: ~/ai-builder-output)

    Returns:
        Dict with project details: project_name, description, directory,
        files_created, setup_instructions, model_used, model_reason,
        cost_usd, readme
    """
    client = _get_client()

    # Route to the appropriate model for build tasks
    # Build tasks always use at least Sonnet
    model_id, model_reason = route_model(prompt)

    # For build tasks, ensure at least Sonnet level
    if "haiku" in model_id:
        model_id = MODEL_SONNET
        model_reason = "Build tasks require Sonnet or higher"

    logger.info(f"Building project with {model_id}: {prompt[:80]}...")

    # Call Claude API
    message = client.messages.create(
        model=model_id,
        max_tokens=8000,
        system=SYSTEM_BUILDER,
        messages=[
            {
                "role": "user",
                "content": f"Build a complete project for: {prompt}",
            }
        ],
    )

    raw_response = message.content[0].text

    # Parse the JSON response
    cleaned = _strip_json_fences(raw_response)
    try:
        project_data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse project JSON: {e}")
        logger.error(f"Raw response (first 500 chars): {raw_response[:500]}")
        raise ValueError(f"Claude returned invalid JSON: {e}")

    # Validate required keys
    required_keys = ["project_name", "description", "files", "setup_instructions", "readme"]
    for key in required_keys:
        if key not in project_data:
            raise ValueError(f"Missing required key in project JSON: {key}")

    # Determine output directory
    base_dir = output_base if output_base else os.getenv("OUTPUT_DIR")
    if base_dir:
        base_dir = os.path.expanduser(base_dir)
    else:
        base_dir = DEFAULT_OUTPUT_DIR

    project_name = project_data["project_name"]
    project_dir = os.path.join(base_dir, project_name)

    # Create project directory and write files
    os.makedirs(project_dir, exist_ok=True)
    files_created = []

    for file_info in project_data["files"]:
        file_path = os.path.join(project_dir, file_info["path"])
        parent_dir = os.path.dirname(file_path)

        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(file_info["content"])

        files_created.append(os.path.abspath(file_path))
        logger.info(f"  Created: {file_info['path']}")

    # Write README if not already in files
    readme_exists = any(f["path"].lower() == "readme.md" for f in project_data["files"])
    if not readme_exists and project_data.get("readme"):
        readme_path = os.path.join(project_dir, "README.md")
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(project_data["readme"])
        files_created.append(os.path.abspath(readme_path))

    # Calculate cost
    usage = message.usage
    cost_usd = estimate_cost(usage.input_tokens, usage.output_tokens, model_id)

    result = {
        "project_name": project_name,
        "description": project_data["description"],
        "directory": os.path.abspath(project_dir),
        "files_created": files_created,
        "setup_instructions": project_data["setup_instructions"],
        "model_used": get_model_alias(model_id),
        "model_reason": model_reason,
        "cost_usd": cost_usd,
        "readme": project_data.get("readme", ""),
    }

    logger.info(f"Project '{project_name}' built successfully in {project_dir}")
    return result


def qa_review(project: dict) -> str:
    """Run a quick QA review on the generated project files.

    Args:
        project: The project dict returned by build_project()

    Returns:
        Review text from Claude
    """
    client = _get_client()

    # Collect file contents for review (up to 5 files, 500 chars each)
    file_summaries = []
    files_to_review = project.get("files_created", [])[:5]

    for file_path in files_to_review:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read(500)
            filename = os.path.basename(file_path)
            file_summaries.append(f"--- {filename} ---\n{content}")
        except Exception as e:
            logger.warning(f"Could not read {file_path}: {e}")

    if not file_summaries:
        return "No files available for review."

    review_content = "\n\n".join(file_summaries)

    message = client.messages.create(
        model=MODEL_HAIKU,
        max_tokens=500,
        system=SYSTEM_QA,
        messages=[
            {
                "role": "user",
                "content": f"Review this project '{project['project_name']}':\n\n{review_content}",
            }
        ],
    )

    return message.content[0].text
