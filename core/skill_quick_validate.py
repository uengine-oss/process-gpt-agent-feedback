"""
PyYAML-free skill validator for skill-creator workflow.
Validates SKILL.md frontmatter (name, description) per skill-creator spec.
Used inside computer-use Pod and can be run locally when skill_path is a local dir.
"""

import re
from pathlib import Path


def _parse_simple_frontmatter(text: str) -> dict:
    """Minimal frontmatter parser for name and description. No PyYAML."""
    d = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+(?:-\w+)*):\s*(.+)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1].replace('\\"', '"')
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1].replace("\\'", "'")
            d[key] = val
    return d


def validate_skill(skill_path: str | Path) -> tuple[bool, str]:
    """
    Validate a skill directory (SKILL.md frontmatter: name, description).

    Args:
        skill_path: Path to the skill directory (must contain SKILL.md).

    Returns:
        (True, "Skill is valid!") or (False, "error message").
    """
    skill_path = Path(skill_path)

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"

    content = skill_md.read_text(encoding="utf-8", errors="replace")
    if not content.startswith("---"):
        return False, "No YAML frontmatter found"

    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format"

    frontmatter = _parse_simple_frontmatter(match.group(1))

    ALLOWED = {"name", "description", "license", "allowed-tools", "metadata"}
    unexpected = set(frontmatter.keys()) - ALLOWED
    if unexpected:
        return False, (
            f"Unexpected key(s) in SKILL.md frontmatter: {', '.join(sorted(unexpected))}. "
            f"Allowed: {', '.join(sorted(ALLOWED))}"
        )

    if "name" not in frontmatter:
        return False, "Missing 'name' in frontmatter"
    if "description" not in frontmatter:
        return False, "Missing 'description' in frontmatter"

    name = frontmatter.get("name", "")
    if not isinstance(name, str):
        return False, f"Name must be a string, got {type(name).__name__}"
    name = name.strip()
    if name:
        if not re.match(r"^[a-z0-9-]+$", name):
            return False, f"Name '{name}' should be hyphen-case (lowercase, digits, hyphens only)"
        if name.startswith("-") or name.endswith("-") or "--" in name:
            return False, f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens"
        if len(name) > 64:
            return False, f"Name too long ({len(name)} chars). Max 64."

    description = frontmatter.get("description", "")
    if not isinstance(description, str):
        return False, f"Description must be a string, got {type(description).__name__}"
    description = description.strip()
    if description:
        if "<" in description or ">" in description:
            return False, "Description cannot contain angle brackets (< or >)"
        if len(description) > 1024:
            return False, f"Description too long ({len(description)} chars). Max 1024."

    return True, "Skill is valid!"


def get_quick_validate_script() -> str:
    """
    Return the full content of quick_validate.py for use inside the computer-use Pod.
    package_skill.py does 'from quick_validate import validate_skill'; this script
    must define validate_skill(skill_path) and support being run as:
      python quick_validate.py <skill_directory>
    """
    return r'''#!/usr/bin/env python3
"""Minimal skill validator (no PyYAML). From skill_quick_validate.get_quick_validate_script()."""

import re
import sys
from pathlib import Path


def _parse_simple_frontmatter(text):
    d = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+(?:-\w+)*):\s*(.+)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1].replace(r'\"', '"')
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1].replace(r"\'", "'")
            d[key] = val
    return d


def validate_skill(skill_path):
    skill_path = Path(skill_path)
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"
    content = skill_md.read_text(encoding="utf-8", errors="replace")
    if not content.startswith("---"):
        return False, "No YAML frontmatter found"
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format"
    frontmatter = _parse_simple_frontmatter(match.group(1))
    ALLOWED = {"name", "description", "license", "allowed-tools", "metadata"}
    unexpected = set(frontmatter.keys()) - ALLOWED
    if unexpected:
        return False, "Unexpected frontmatter keys: " + ", ".join(sorted(unexpected))
    if "name" not in frontmatter or "description" not in frontmatter:
        return False, "Missing name or description in frontmatter"
    name = str(frontmatter.get("name", "")).strip()
    if name and not re.match(r"^[a-z0-9-]+$", name):
        return False, "Name must be hyphen-case"
    if name and (name.startswith("-") or name.endswith("-") or "--" in name):
        return False, "Name cannot start/end with hyphen or have consecutive hyphens"
    if name and len(name) > 64:
        return False, "Name too long"
    desc = str(frontmatter.get("description", "")).strip()
    if desc and ("<" in desc or ">" in desc):
        return False, "Description cannot contain < or >"
    if desc and len(desc) > 1024:
        return False, "Description too long"
    return True, "Skill is valid!"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python quick_validate.py <skill_directory>")
        sys.exit(1)
    ok, msg = validate_skill(sys.argv[1])
    print(msg)
    sys.exit(0 if ok else 1)
'''
