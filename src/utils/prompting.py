"""为 blind repair agent 构造最小可见信息的 design issue prompt。"""

from __future__ import annotations


def build_design_issue_prompt(*, repo: str, problem_statement: str, filename: str | None) -> str:
    location_line = (
        f"- The issue comment appears in `{filename}`. Start there, but do not assume the full fix is local to that function or file."
        if filename
        else "- The exact file containing the issue comment is not available."
    )
    return (
        f"You are fixing a design issue in the current checkout of `{repo}`.\n\n"
        "Task:\n"
        "- Read the codebase and implement a production-code fix for the issue described below.\n"
        "- Treat this as a design issue: the right change may require edits across multiple functions or files.\n"
        "- Do not limit yourself to the function containing the original comment if the real fix belongs elsewhere.\n"
        "- Focus on production code. Do not add or modify tests unless that is absolutely required to keep the codebase coherent.\n"
        "- Work only with the current checkout. Do not rely on web resources, hidden benchmark assets, or future repository state.\n"
        "- Do not use shell commands or tools to access the network, browse the web, or fetch remote resources.\n"
        "- Keep the change focused on the issue and avoid unrelated cleanup.\n\n"
        "Issue description:\n"
        f"- {problem_statement.strip()}\n"
        f"{location_line}\n\n"
        "Working rules:\n"
        "- Inspect the repository, reason about how the current behavior fails to satisfy the issue description, and then modify the code.\n"
        "- Before editing, identify the minimal set of production files that must change. Prefer targeted reads of those files over broad repeated searches once the likely fix area is known.\n"
        "- Modify files directly in the working tree. Do not return git patches, diffs, apply_patch blocks, or textual edit instructions as a substitute for real file edits.\n"
        "- Prefer robust editing methods. When you need to edit a file via the shell, prefer direct file editing tools or a short Python script passed via stdin (for example `python - <<'PY'`) that reads and writes the target file safely.\n"
        "- Do not create rewrite scripts with shell here-doc patterns like `cat <<'EOF' > file`, and do not embed source text containing backticks, quotes, braces, or multiline code excerpts directly inside shell command strings.\n"
        "- Avoid fragile shell text-substitution pipelines when editing code. If a change depends on matching source text, implement it in Python so the source text is treated as plain data rather than shell syntax.\n"
        "- After each edit attempt, verify the target file contents directly before moving on. If an edit command fails or the file content does not match your intent, fix the edit instead of continuing with more searches.\n"
        "- If the repository is not writable or file edits fail, stop the repair without producing a patch in the response. Leave the working tree unchanged.\n"
        "- The correct fix may require edits across multiple functions or files.\n"
        "- Stop after the code changes are in the working tree.\n"
        "- In the final note, briefly list the files you changed and why.\n"
    ).strip()
