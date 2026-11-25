"""モデルの中の「使われていない基準平面」と「無駄なスケッチ」を数える（点検用）。何も変更しない。

    python -m swkit.audit <部品またはアセンブリ> [...]
    python -m swkit.audit <フォルダ>          配下の部品・アセンブリをすべて見る

見るもの
  基準平面: 既定の 3 平面を除き、子（それを使うスケッチ・フィーチャ・合致）が 1 つも無いもの
  スケッチ: 線も点も無い空のスケッチ、どのフィーチャにも使われていないスケッチ（名前を付けて残したレイアウトは除く）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from .core import SW, call0, M


def audit(sw, path):
    doc = sw.open(path)
    planes, sketches, dead_planes, empty, unused = 0, 0, [], [], []
    f, n_default = call0(doc, "FirstFeature"), 0
    while f is not None:
        t = call0(f, "GetTypeName2")
        if t == "RefPlane":
            n_default += 1
            if n_default > 3:                         # 先頭 3 枚は既定の平面
                planes += 1
                kids = call0(f, "GetChildren")
                if not kids:
                    dead_planes.append(f.Name)
        elif t in ("ProfileFeature", "3DProfileFeature"):
            sketches += 1
            sk = call0(f, "GetSpecificFeature2")
            segs = call0(sk, "GetSketchSegments") if sk is not None else None
            pts = call0(sk, "GetSketchPoints2") if sk is not None else None
            if not segs and not pts:
                empty.append(f.Name)
            elif not call0(f, "GetChildren") and re.fullmatch(r"(ｽｹｯﾁ|スケッチ|Sketch)\s*\d+", f.Name):
                unused.append(f.Name)                 # 名前を付けて残したレイアウトスケッチは数えない
        f = call0(f, "GetNextFeature")
    title = call0(doc, "GetTitle")
    sw.app.CloseDoc(title)
    return {"文書": Path(path).name, "基準平面": planes, "使われていない基準平面": dead_planes, "スケッチ": sketches,
            "空のスケッチ": empty, "使われていないスケッチ": unused}


def main():
    targets = []
    for a in sys.argv[1:]:
        p = Path(a)
        targets += sorted(x for x in p.rglob("*") if x.suffix.upper() in (".SLDPRT", ".SLDASM") and not x.name.startswith("~$")) if p.is_dir() else [p]
    sw = SW()
    tot = {"基準平面": 0, "使われていない基準平面": 0, "スケッチ": 0, "空のスケッチ": 0, "使われていないスケッチ": 0}
    try:
        for t in targets:
            r = audit(sw, t.resolve())
            for k in tot:
                tot[k] += r[k] if isinstance(r[k], int) else len(r[k])
            bad = {k: v for k, v in r.items() if isinstance(v, list) and v}
            print(f"{r['文書']:<36} 基準平面 {r['基準平面']:>4}  スケッチ {r['スケッチ']:>3}" + ("" if not bad else "  ← " + "; ".join(f"{k} {len(v)}: {', '.join(v[:6])}{' …' if len(v) > 6 else ''}" for k, v in bad.items())))
    finally:
        sw.restore()
    print("合計", tot)


if __name__ == "__main__":
    main()
