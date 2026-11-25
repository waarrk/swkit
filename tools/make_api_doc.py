"""docs/api.md を作る．公開しているクラスと関数の引数と説明を，ソースから抜き出して一覧にする．

    python tools/make_api_doc.py
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "swkit"
MODULES = [("core", "SOLIDWORKS の操作"), ("project", "設計の手順"), ("vendor", "製造元の配布データ"), ("rapid", "MISUMI RAPiD Design"),
           ("docgen", "正式文書の生成"), ("audit", "モデルの点検"), ("cli", "コマンド"), ("config", "設定")]


def first_line(node):
    d = ast.get_docstring(node) or ""
    return d.strip().split("\n")[0].replace("|", "／")


def sig(fn):
    a = fn.args
    names = [x.arg for x in a.args if x.arg not in ("self", "cls")]
    n_def = len(a.defaults)
    out = []
    for i, n in enumerate(names):
        k = i - (len(names) - n_def)
        out.append(f"{n}={ast.unparse(a.defaults[k])}" if k >= 0 else n)
    if a.vararg:
        out.append("*" + a.vararg.arg)
    if a.kwarg:
        out.append("**" + a.kwarg.arg)
    return ", ".join(out)


def main():
    lines = ["# API 一覧", "", "ソースから自動で生成した一覧．説明は各関数の説明文の 1行目である．名前が `_` で始まるものは内部用のため載せていない．", ""]
    for mod, title in MODULES:
        tree = ast.parse((SRC / f"{mod}.py").read_text(encoding="utf-8"))
        lines += [f"## swkit.{mod}", "", first_line(tree) or title, ""]
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]
        if funcs:
            lines += ["| 関数 | 説明 |", "| --- | --- |"]
            lines += [f"| `{f.name}({sig(f)})` | {first_line(f)} |" for f in funcs]
            lines.append("")
        for c in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            methods = [n for n in c.body if isinstance(n, ast.FunctionDef) and (not n.name.startswith("_") or n.name == "__init__")]
            if not methods:
                continue
            lines += [f"### {c.name}", "", first_line(c), "", "| メソッド | 説明 |", "| --- | --- |"]
            lines += [f"| `{m.name}({sig(m)})` | {first_line(m)} |" for m in methods]
            lines.append("")
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "api.md").write_text("\n".join(lines), encoding="utf-8")
    print("docs/api.md", len(lines), "行")


if __name__ == "__main__":
    main()
