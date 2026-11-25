"""例: 板の外形をエンティティ変換し、内側へオフセットした輪郭で窓を抜く。

    python examples/plate_with_offset_window.py [保存先フォルダ]

設計値は名前付きの変数（P）に置き、スケッチの寸法は関係式でその変数につながる。
"""
import sys
import types
from pathlib import Path

from swkit import SW

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
P = types.SimpleNamespace(W=120.0, H=80.0, T=10.0, RIM=15.0)
sw = SW(P)
try:
    p = sw.new_part()
    s = p.sketch("front")
    s.rect("-W / 2", "W / 2", "-H / 2", "H / 2")
    p.extrude_mid("T", "板")
    s = p.sketch("front")
    src = s.convert([(0, "H / 2", "T / 2"), ("W / 2", 0, "T / 2"), (0, "-H / 2", "T / 2"), ("-W / 2", 0, "T / 2")], construction=True)
    new = s.offset(src, "RIM", (0, 0))
    print("変換", len(src), "本 / オフセット", len(new), "本 / 状態", s.sk.GetConstrainedStatus)
    for g in new:
        a, b = g.GetStartPoint2, g.GetEndPoint2
        print("   ", round(a.X * 1000, 2), round(a.Y * 1000, 2), "→", round(b.X * 1000, 2), round(b.Y * 1000, 2))
    p.cut_through("窓（外形からオフセット）")
    p.solve_equations()
    eq = p.eq
    print([eq.Equation(i) for i in range(eq.GetCount)])
    print("体積 mm3", round(p.mass()[1] * 1e9), "期待", (120 * 80 - 90 * 50) * 10)
    p.save_as(OUT / "plate_with_offset_window.SLDPRT")
    print("警告", getattr(p, "warnings", None))
finally:
    sw.restore()
