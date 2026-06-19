"""
Pydantic models for structured task outputs.

These are used as output_pydantic on each CrewAI Task so that:
- The LLM is forced to produce valid JSON matching the schema
- The next agent receives structured context instead of free text
- Python validators can check required fields (files_read, evidence, etc.)
"""
from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field


class Issue(BaseModel):
    """A problem found in the code, with mandatory evidence."""
    file: str = Field(description="Absolute path to the file containing the issue")
    line: str = Field(default="", description="Line number or range, e.g. '42' or '42-55'")
    description: str = Field(description="Clear description of the issue")
    snippet: str = Field(default="", description="Exact code as it currently exists in the file (verbatim, no diff markers) — used as old_snippet for patching. Proof of the issue.")
    patch_after: str = Field(default="", description="Exact replacement code (corrected version, verbatim, no diff markers) — used as new_snippet for patching. Empty only when replacement cannot be determined from analysis alone.")
    severity: str = Field(default="medium", description="low | medium | high | critical")


class FileChange(BaseModel):
    """A file to create or modify as part of the plan."""
    path: str = Field(description="Absolute path to the file")
    action: str = Field(description="create | modify")
    reason: str = Field(default="", description="Why this change is needed")
    location: str = Field(default="", description="Function / component / line where the change goes")


class ArchitecturePlan(BaseModel):
    """Structured output of design_task.

    Passed as JSON context to the next agent (developer or reviewer).
    Python validation: files_read must not be empty — if it is, the architect
    did not read any files and the plan is based on assumptions.
    """
    files_read: list[str] = Field(
        default_factory=list,
        description="Absolute paths of ALL files read with read_file during analysis",
    )
    issues: list[Issue] = Field(
        default_factory=list,
        description="All issues found. Each MUST have a snippet copied from read_file as proof",
    )
    changes: list[FileChange] = Field(
        default_factory=list,
        description="Files to create or modify, with exact location of each change",
    )
    endpoints_verified: list[str] = Field(
        default_factory=list,
        description="Verified API endpoints, e.g. 'GET /api/users (router.py:42)'",
    )
    plan: str = Field(
        default="",
        description="Full implementation plan in Portuguese markdown with ANTES/DEPOIS code blocks",
    )


class ImplementationResult(BaseModel):
    """Structured output of implement_task and fix_task."""
    files_modified: list[str] = Field(
        default_factory=list,
        description="Absolute paths of files modified with patch_file",
    )
    files_created: list[str] = Field(
        default_factory=list,
        description="Absolute paths of files created with write_file",
    )
    syntax_errors: list[Issue] = Field(
        default_factory=list,
        description="Syntax errors found by validate_python_syntax / validate_typescript / validate_powershell",
    )
    summary: str = Field(
        default="",
        description="Implementation summary in Portuguese markdown with ANTES/DEPOIS blocks for each change",
    )


class ReviewResult(BaseModel):
    """Structured output of review_task.

    approved=True requires issues to be empty.
    approved=False requires at least one issue with file+line+snippet evidence.
    Any issue without a snippet is invalid — no proof, no issue.
    """
    approved: bool = Field(
        description="True if all checks pass. False if any issue requires fixing",
    )
    issues: list[Issue] = Field(
        default_factory=list,
        description="Issues found. Each MUST have file, line, and snippet as proof. No snippet = not valid",
    )
    tests_passed: Optional[bool] = Field(
        default=None,
        description="True if tests pass, False if they fail, null if no test suite exists",
    )
    verdict: str = Field(
        description="'Aprovado' or 'Requer correções: <brief reason>'",
    )
    summary: str = Field(
        default="",
        description="Full review report in Portuguese markdown",
    )
