"""メーカー配布の 3D データ（STEP / IGES / SLDPRT）を調べて、既製品の部品として取り込む。

    python src/swkit/vendor.py <ファイル> [<ファイル> ...]     形状の概要（外形寸法・円筒面の軸）を表示する

配布データは原点も向きもまちまちなので、取り込んだあとボディを動かして、部品の原点と基準平面を
「軸 = X、取付面 = 原点」のような決まった向きにそろえる。そろえ方は align() に渡す。
"""
from __future__ import annotations

import math
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from swkit.core import MM, NOTHING, SW, M, _byref_int, call0  # noqa: E402


def load(sw: SW, path):
    """STEP / IGES / SLDPRT を開いて文書を返す。"""
    path = str(Path(path).resolve())
    if path.lower().endswith(".sldprt"):
        return sw.open(path)
    e, w = _byref_int(), _byref_int()
    doc = sw.app.OpenDoc6(path, 1, 1, "", e, w)          # 中間形式もそのまま部品として開く
    if doc is None:
        data = M(sw.app, "GetImportFileData", path)
        doc = M(sw.app, "LoadFile4", path, "r", data, e)
    if doc is None:
        raise RuntimeError(f"取り込めない: {path} err={e.value}")
    return doc


def bodies(doc):
    """ソリッドとサーフェスの全ボディ。

    IGES などはソリッドにならず面のまま入る形状（羽根など）がある。ソリッドだけを動かすと面が元の位置に取り残される。
    """
    return [b for kind in (0, 1) for b in (M(doc, "GetBodies2", kind, False) or ())]


def bbox(doc):
    """全ボディを囲む箱 (min xyz, max xyz) mm。"""
    lo, hi = [1e9] * 3, [-1e9] * 3
    for b in bodies(doc):
        box = call0(b, "GetBodyBox")
        for i in range(3):
            lo[i], hi[i] = min(lo[i], box[i] / MM), max(hi[i], box[i + 3] / MM)
    return lo, hi


def cylinders(doc, min_r=1.0):
    """円筒面の (半径 mm, 軸上の点 mm, 軸方向) を面積の大きい順に返す。"""
    out = []
    for b in bodies(doc):
        for f in (call0(b, "GetFaces") or ()):
            s = call0(f, "GetSurface")
            if call0(s, "IsCylinder"):
                p = s.CylinderParams
                r = p[6] / MM
                if r >= min_r:
                    out.append((call0(f, "GetArea"), round(r, 3), tuple(round(c / MM, 3) for c in p[:3]), tuple(round(c, 4) for c in p[3:6])))
    out.sort(reverse=True)
    return [(r, o, a) for _, r, o, a in out]


def planes(doc, axis, tol=1e-3):
    """法線が axis（0/1/2）に平行な平面の、軸方向の位置 mm → 面積 mm2。"""
    pos = Counter()
    for b in bodies(doc):
        for f in (call0(b, "GetFaces") or ()):
            s = call0(f, "GetSurface")
            if call0(s, "IsPlane"):
                p = s.PlaneParams
                if abs(abs(p[axis]) - 1) < tol:
                    pos[round(p[3 + axis] / MM, 2)] += call0(f, "GetArea") / MM ** 2
    return dict(sorted(pos.items()))


def _box_after(lo, hi, rows, origin):
    """箱の 8 隅を p' = R (p - origin) で写したあとの箱。"""
    pts = []
    for x in (lo[0], hi[0]):
        for y in (lo[1], hi[1]):
            for z in (lo[2], hi[2]):
                q = (x - origin[0], y - origin[1], z - origin[2])
                pts.append([sum(rows[i][k] * q[k] for k in range(3)) for i in range(3)])
    return [min(p[i] for p in pts) for i in range(3)], [max(p[i] for p in pts) for i in range(3)]


def align(doc, rows, origin):
    """ボディを動かして、部品の原点と向きを決まった形にそろえる。

    rows: いまの座標系で見た「そろえたあとの X, Y, Z 軸」の向き（単位ベクトル 3 本）。
    origin: いまの座標系で見た、そろえたあとの原点の位置 mm。
    「ボディの移動/コピー」フィーチャで、原点へ平行移動してから X → Y → Z 軸まわりに 1 軸ずつ回す。
    回転の向きの決まりは API の説明に頼らず、動かしたあとの外形の箱が計算どおりかで確かめる。
    """
    r = rows
    by = -math.asin(max(-1.0, min(1.0, r[2][0])))        # R = Rz(c) Ry(b) Rx(a)
    if abs(math.cos(by)) > 1e-6:
        ax, cz = math.atan2(r[2][1], r[2][2]), math.atan2(r[1][0], r[0][0])
    else:
        ax, cz = math.atan2(-r[1][2], r[1][1]), 0.0
    want = _box_after(*bbox(doc), rows, origin)
    fm = doc.FeatureManager

    def move(name, *args):
        doc.ClearSelection2(True)
        for body in bodies(doc):
            sd = call0(doc.SelectionManager, "CreateSelectData")
            sd.Mark = 1
            M(body, "Select2", True, sd)
        f = M(fm, "InsertMoveCopyBody2", *args)
        if f is None:
            raise RuntimeError(f"ボディを動かせない: {name}")
        f.Name = name
        return f

    def undo(feats):
        for f in reversed(feats):
            doc.ClearSelection2(True)
            f.Select2(False, 0)
            M(doc, "EditDelete")

    move("原点合わせ", -origin[0] * MM, -origin[1] * MM, -origin[2] * MM, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, False, 1)
    for sign in (1.0, -1.0):
        done = []
        for name, ang in (("向き合わせ X", (sign * ax, 0.0, 0.0)), ("向き合わせ Y", (0.0, sign * by, 0.0)), ("向き合わせ Z", (0.0, 0.0, sign * cz))):
            if abs(sum(ang)) > 1e-9:
                # 回転角の引数は Z, Y, X 軸まわりの順（引数名は X, Y, Z だが、実際の動きで確かめた）
                done.append(move(name, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ang[2], ang[1], ang[0], False, 1))
        doc.ForceRebuild3(False)
        got = bbox(doc)
        if all(abs(got[k][i] - want[k][i]) < 0.05 for k in range(2) for i in range(3)):
            doc.ClearSelection2(True)
            return
        undo(done)
    raise RuntimeError("向きをそろえられない（外形の箱が計算と合わない）")


def main():
    sw = SW()
    try:
        for path in sys.argv[1:]:
            doc = load(sw, path)
            lo, hi = bbox(doc)
            print(f"== {Path(path).name}: ボディ {len(bodies(doc))} 個")
            print("   箱 min", [round(v, 2) for v in lo], "max", [round(v, 2) for v in hi], "寸法", [round(h - l, 2) for l, h in zip(lo, hi)])
            seen = set()
            for r, o, a in cylinders(doc):
                key = (r, tuple(abs(c) for c in a))
                if key not in seen and len(seen) < 8:
                    seen.add(key)
                    print(f"   円筒 r={r:<8} 軸上の点 {o} 向き {a}")
            for ax in range(3):
                pl = planes(doc, ax)
                big = sorted(pl.items(), key=lambda kv: -kv[1])[:6]
                print(f"   {'XYZ'[ax]} に垂直な平面（位置: 面積）", {k: round(v) for k, v in sorted(big)})
    finally:
        sw.restore()


if __name__ == "__main__":
    main()
