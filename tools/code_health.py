"""Weekly code-health sweep: reads the Python source with the standard ``ast`` module and reports smells.

    python tools/code_health.py              # print the report; exit 1 if a rule marked "error" is broken
    python tools/code_health.py --markdown   # the same, as a Markdown report (for the weekly workflow)

Errors (they fail the sweep, and tests/test_code_structure.py runs the same checks):

* A bare ``except:``. It also catches KeyboardInterrupt and SystemExit and hides real faults.
* Money sizing done with floats. Functions listed in ``DECIMAL_SIZING`` turn account values into
  order quantities; they must use ``Decimal`` and must not call ``float()``, ``math.floor`` or ``round``.
* Two functions with the same body (6 statements or more) in different places: one should call the other.

Reported but not failing:

* Functions longer than ``LONG_FUNCTION`` lines: candidates to split.

The sweep proposes nothing and changes nothing. Fixes are made by a person (or a coding assistant working
for one) on a branch, and reach the site only through the full test suite and a reviewed pull request.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterator, List, Tuple, Union

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("loki", "loki_v8", "core")
LONG_FUNCTION = 150
DUPLICATE_MIN_STATEMENTS = 6
# Functions that turn account values into order quantities.
DECIMAL_SIZING = {("loki/api/alpaca_daytrade.py", "target_shares")}
FLOAT_CALLS = {"float", "round"}


def sources() -> List[Path]:
    files = [p for folder in FOLDERS for p in (ROOT / folder).rglob("*.py")]
    files += [p for p in ROOT.glob("*.py")]
    return sorted(files)


def _functions(tree: ast.AST) -> Iterator[Union[ast.FunctionDef, ast.AsyncFunctionDef]]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def sweep() -> Dict[str, List[str]]:
    errors: List[str] = []
    notes: List[str] = []
    bodies: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for path in sources():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                errors.append(f"{rel}:{node.lineno}: bare 'except:' (name the exceptions, or use 'except Exception')")
        for fn in _functions(tree):
            length = (fn.end_lineno or fn.lineno) - fn.lineno + 1
            if length > LONG_FUNCTION:
                notes.append(f"{rel}:{fn.lineno}: {fn.name}() is {length} lines long")
            if (rel, fn.name) in DECIMAL_SIZING:
                names = {n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
                attrs = {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(fn)
                         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)}
                if "Decimal" not in names:
                    errors.append(f"{rel}:{fn.lineno}: {fn.name}() sizes orders without Decimal")
                for bad in sorted(names & FLOAT_CALLS | ({"math.floor"} & attrs)):
                    errors.append(f"{rel}:{fn.lineno}: {fn.name}() sizes orders with {bad}()")
            if len(fn.body) >= DUPLICATE_MIN_STATEMENTS:
                body = ast.dump(ast.Module(body=fn.body, type_ignores=[]), annotate_fields=False)
                bodies[hashlib.sha256(body.encode()).hexdigest()].append((rel, f"{fn.name}():{fn.lineno}"))
    for places in bodies.values():
        if len(places) > 1:
            errors.append("same function body in " + ", ".join(f"{r} {f}" for r, f in places))
    return {"errors": errors, "notes": notes}


def report(result: Dict[str, List[str]], markdown: bool) -> str:
    if not markdown:
        lines = [f"ERROR {e}" for e in result["errors"]] + [f"note  {n}" for n in result["notes"]]
        return "\n".join(lines + [f"{len(result['errors'])} error(s), {len(result['notes'])} note(s)"])
    lines = ["## Code health", "", f"**{len(result['errors'])} error(s), {len(result['notes'])} note(s).**", ""]
    if result["errors"]:
        lines += ["### Errors", ""] + [f"- `{e}`" for e in result["errors"]] + [""]
    if result["notes"]:
        lines += [f"### Long functions (over {LONG_FUNCTION} lines)", ""] + [f"- `{n}`" for n in result["notes"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--markdown", action="store_true")
    args = parser.parse_args()
    result = sweep()
    print(report(result, args.markdown))
    sys.exit(1 if result["errors"] else 0)


if __name__ == "__main__":
    main()
