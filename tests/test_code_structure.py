"""House rules that keep the code tidy: every file says what it is for, and no browser
script leaks helper names to another."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted(p for p in (ROOT / "static" / "js").rglob("*.js"))
# Scripts that are deliberately not wrapped in a function, and why.
NOT_WRAPPED = {
    "mc_worker.js": "a Web Worker: its global scope is its own",
    "game_data.js": "a data file: one assignment to window.LokiDiveBank",
}
WRAPPED = re.compile(r"\(\s*(async\s*)?(\(\s*\)\s*=>|function\b)")


def _split_header(source: str):
    """(has a leading comment, the first real code) for a script or stylesheet."""
    rest, labelled = source, False
    while True:
        rest = rest.lstrip()
        if rest.startswith("/*"):
            labelled, rest = True, rest[rest.index("*/") + 2:]
        elif rest.startswith("//"):
            labelled, rest = True, rest.partition("\n")[2]
        elif re.match(r"['\"]use strict['\"];?", rest):
            rest = re.sub(r"^['\"]use strict['\"];?", "", rest)
        else:
            return labelled, rest


def test_every_python_module_says_what_it_is_for():
    modules = [p for folder in ("loki", "loki_v8", "core", "tests", "tools") for p in (ROOT / folder).rglob("*.py")]
    modules += list(ROOT.glob("*.py"))
    unlabelled = [str(p.relative_to(ROOT)) for p in modules if not ast.get_docstring(ast.parse(p.read_text(encoding="utf-8")))]
    assert unlabelled == [], f"add a module docstring: {unlabelled}"


def test_every_script_and_stylesheet_starts_with_a_description():
    files = SCRIPTS + sorted((ROOT / "static" / "css").glob("*.css"))
    assert len(SCRIPTS) > 60
    for path in files:
        source = path.read_text(encoding="utf-8")
        assert not source.startswith("﻿"), f"{path.name}: remove the byte-order mark"
        assert _split_header(source)[0], f"{path.name}: start the file with a comment saying what it is for"


def test_no_browser_script_leaks_names_to_another():
    for path in SCRIPTS:
        if path.name in NOT_WRAPPED:
            continue
        code = _split_header(path.read_text(encoding="utf-8"))[1]
        assert WRAPPED.match(code), f"{path.name}: wrap the script in (() => {{ ... }})(); so nothing it declares is global"


def test_shared_workspace_helpers_are_taken_by_name_and_all_exist():
    workspace = (ROOT / "static" / "js" / "workspace.js").read_text(encoding="utf-8")
    block = re.search(r"window\.LokiWorkspace\s*=\s*Object\.freeze\(\{(.*?)\}\);", workspace, re.S)
    assert block, "workspace.js must hand its shared helpers out through window.LokiWorkspace"
    offered = set(re.findall(r"(?:^|[,{\s])([A-Za-z_$][\w$]*)\s*(?=[,:}]|$)", block.group(1), re.M))
    assert {"$", "element", "request", "capabilities", "dataset"} <= offered
    users = 0
    for path in SCRIPTS:
        for names in re.findall(r"const\s*\{([^}]*)\}\s*=\s*window\.LokiWorkspace\s*;", path.read_text(encoding="utf-8")):
            users += 1
            taken = {n.strip() for n in names.split(",") if n.strip()}
            assert taken <= offered, f"{path.name} takes {sorted(taken - offered)}, which workspace.js does not offer"
    assert users >= 9  # the workspace scripts all declare what they use


def test_the_code_health_sweep_finds_no_errors():
    import importlib.util
    spec = importlib.util.spec_from_file_location("code_health", ROOT / "tools" / "code_health.py")
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    result = health.sweep()
    assert result["errors"] == [], "\n".join(result["errors"])


def test_runtime_requirements_are_pinned_exactly():
    """Production installs exactly the versions the suite passed on: no '>=' ranges in requirements.txt."""
    root = Path(__file__).resolve().parents[1]
    lines = [line.split("#", 1)[0].strip() for line in (root / "requirements.txt").read_text().splitlines()]
    packages = [line for line in lines if line]
    assert packages and all("==" in line and not any(op in line for op in (">", "<", "~", "!")) for line in packages), packages
    import tomllib
    declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert sorted(declared) == sorted(packages)  # Vercel may install from either file: they must say the same
