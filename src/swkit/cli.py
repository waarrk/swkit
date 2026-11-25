"""swkit のコマンド。

    swkit doctor            環境の点検（SOLIDWORKS、テンプレート、pywin32、RAPiD Design、設計者スキル）
    swkit setup             この PC 用の設定（~/.swkit.json）を作り、設計者スキルを入れる
    swkit new <名前> [場所]  設計プロジェクトの骨組みを作る
    swkit skill             設計者スキル（Claude Code 用）だけを入れ直す

どの PC でも `pip install` のあと `swkit setup` を 1 回実行すれば、同じ決まりで設計を始められる。
"""
from __future__ import annotations

import json
import shutil
import sys
import winreg
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
CONFIG = Path.home() / ".swkit.json"
SKILL_DIR = Path.home() / ".claude" / "skills" / "swkit-designer"
RAPID_DLL = Path(r"C:\MiSUMi RAPiD Design\Components\SW\bin\MisumiCatalogTools.dll")
YEAR0 = 1992          # SldWorks.Application.NN の NN + 1992 = 版（32 → 2024）


def installed_solidworks():
    """入っている SOLIDWORKS の一覧 [(COM 名, 年, 実行ファイル)]。新しい順。"""
    out = []
    for n in range(40, 20, -1):
        pid = f"SldWorks.Application.{n}"
        try:
            clsid = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, pid + r"\CLSID")
            exe = winreg.QueryValue(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32").split(" /")[0].strip('"')
        except OSError:
            continue
        if Path(exe).exists():
            out.append((pid, YEAR0 + n, exe))
    return out


def find_templates(exe):
    """SOLIDWORKS の既定の場所から部品・アセンブリのテンプレートを探す。見つからなければ空文字。"""
    roots = [Path(r"C:\ProgramData\SOLIDWORKS"), Path(r"C:\ProgramData\SolidWorks"), Path(exe).parent / "lang"]
    part = asm = ""
    for root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*.prtdot"):
            part = part or str(f)
        for f in root.rglob("*.asmdot"):
            asm = asm or str(f)
    return part, asm


def load_config():
    return json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}


def doctor():
    ok = True

    def line(good, text, hint=""):
        nonlocal ok
        ok = ok and good
        print(("  [OK] " if good else "  [NG] ") + text + (f"\n       → {hint}" if hint and not good else ""))

    print("swkit doctor")
    line(sys.platform == "win32", f"Windows（{sys.platform}）", "SOLIDWORKS は Windows でしか動かない")
    line(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}", "Python 3.10 以上を使う")
    try:
        import win32com.client  # noqa: F401
        line(True, "pywin32")
    except ImportError:
        line(False, "pywin32 が無い", "pip install pywin32")
    sw = installed_solidworks()
    line(bool(sw), "SOLIDWORKS: " + (", ".join(f"{y}（{p}）" for p, y, _ in sw) or "見つからない"), "SOLIDWORKS をインストールする")
    cfg = load_config()
    line(CONFIG.exists(), f"設定ファイル {CONFIG}" + ("" if CONFIG.exists() else " が無い"), "swkit setup を実行する")
    if cfg:
        pid = cfg.get("progid", "")
        line(any(pid == p for p, _, _ in sw), f"使う版: {pid}", "swkit setup をやり直す")
        for k, label in (("part_template", "部品テンプレート"), ("asm_template", "アセンブリテンプレート")):
            v = cfg.get(k, "")
            if v:
                line(Path(v).exists(), f"{label}: {v}", "パスを直すか、SOLIDWORKS の既定テンプレートを設定して空にする")
            else:
                print(f"  [--] {label}: SOLIDWORKS の既定テンプレートを使う")
    try:
        import pywinauto  # noqa: F401
        gui = True
    except ImportError:
        gui = False
    if RAPID_DLL.exists():
        line(gui, "MISUMI RAPiD Design: あり" + ("" if gui else "（取り込みの自動操作に pywinauto が要る）"), "pip install swkit[rapid]")
    else:
        print("  [--] MISUMI RAPiD Design: 入っていない（ねじ類の取り込みに使う。無くても設計はできる）")
    line((SKILL_DIR / "SKILL.md").exists(), f"設計者スキル: {SKILL_DIR}", "swkit setup を実行する")
    print("問題なし" if ok else "上の [NG] を直してから設計を始める")
    return 0 if ok else 1


def install_skill():
    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEMPLATES / "skill" / "SKILL.md", SKILL_DIR / "SKILL.md")
    shutil.copy2(TEMPLATES / "skill" / "MODELING_RULES.md", SKILL_DIR / "MODELING_RULES.md")
    print(f"設計者スキルを入れた: {SKILL_DIR}")


def setup():
    sw = installed_solidworks()
    if not sw:
        print("SOLIDWORKS が見つからない。インストールしてからやり直す")
        return 1
    cfg = load_config()
    if cfg.get("progid") not in [p for p, _, _ in sw]:
        cfg["progid"] = sw[0][0]                    # いちばん新しい版
    exe = next(e for p, _, e in sw if p == cfg["progid"])
    for k, found in zip(("part_template", "asm_template"), find_templates(exe)):
        if not cfg.get(k) or not Path(cfg[k]).exists():
            cfg[k] = found
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"設定を書いた: {CONFIG}")
    for k, v in cfg.items():
        print(f"  {k}: {v or '（SOLIDWORKS の既定を使う）'}")
    install_skill()
    return doctor()


def new(name, where="."):
    dst = Path(where).resolve() / name
    if dst.exists() and any(dst.iterdir()):
        print(f"{dst} は空でない。別の名前にする")
        return 1
    pkg = "".join(c if c.isalnum() else "_" for c in name).lower()
    for src in (TEMPLATES / "project").rglob("*"):
        rel = Path(str(src.relative_to(TEMPLATES / "project")).replace("__pkg__", pkg))
        out = dst / rel
        if src.is_dir():
            out.mkdir(parents=True, exist_ok=True)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.suffix == ".tmpl":
            out = out.with_suffix("")
        out.write_text(src.read_text(encoding="utf-8").replace("{{name}}", name).replace("{{pkg}}", pkg), encoding="utf-8")
    for d in ("CAD", "CAD_SOURCE/vendor", "Doc/DESIGN_SHEET", "Doc/PART_LIST", "QUOTE"):
        (dst / d).mkdir(parents=True, exist_ok=True)
    print(f"プロジェクトを作った: {dst}")
    print(f"  設計値: ANALYSIS/params.py    生成スクリプト: src/{pkg}/build.py    決まり: CLAUDE.md")
    print(f"  試す: cd {dst} && python src/{pkg}/build.py")
    return 0


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else "help"
    if cmd == "doctor":
        return doctor()
    if cmd == "setup":
        return setup()
    if cmd == "skill":
        install_skill()
        return 0
    if cmd == "new" and len(a) >= 2:
        return new(a[1], a[2] if len(a) > 2 else ".")
    print(__doc__)
    return 0 if cmd in ("help", "-h", "--help") else 1


if __name__ == "__main__":
    sys.exit(main())
