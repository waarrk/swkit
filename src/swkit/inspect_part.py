"""部品のフィーチャ構成・スケッチの定義状態・関係式を一覧する（作り方の点検用）。

    python -m swkit.inspect_part <部品のパス.SLDPRT>
"""
from __future__ import annotations

import sys
from pathlib import Path

from swkit import SW, call0

STATUS = {1: "不明", 2: "未定義", 3: "完全定義", 4: "重複定義", 5: "解なし", 6: "無効"}
SKIP = {"CommentsFolder", "FavoriteFolder", "HistoryFolder", "SelectionSetFolder", "SensorFolder", "DocsFolder",
        "DetailCabinet", "SurfaceBodyFolder", "SolidBodyFolder", "EnvFolder", "InkMarkupFolder", "EqnFolder",
        "MaterialFolder", "AmbientLight", "DirectionLight", "EditBorderFeature", "CutListFolder", "SubWeldFolder",
        "LiveSectionFolder", "MarkupCommentFolder", "BlockFolder", "NotesAreaFtrFolder", "ProfileFtrFolder"}


def main():
    sw = SW()
    try:
        doc = sw.open(Path(sys.argv[1]).resolve())
        print("■ フィーチャ")
        f = call0(doc, "FirstFeature")
        while f is not None:
            t = call0(f, "GetTypeName2")
            if t not in SKIP:
                line = f"  {f.Name:<28} {t}"
                sub = call0(f, "GetFirstSubFeature")
                while sub is not None:
                    if call0(sub, "GetTypeName2") == "ProfileFeature":
                        st = call0(sub, "GetSpecificFeature2").GetConstrainedStatus
                        line += f"   [{sub.Name}: {STATUS.get(st, st)}]"
                    sub = call0(sub, "GetNextSubFeature")
                if t == "ProfileFeature":
                    line += f"   [{STATUS.get(call0(f, 'GetSpecificFeature2').GetConstrainedStatus)}]"
                print(line)
            f = call0(f, "GetNextFeature")
        eq = call0(doc, "GetEquationMgr")
        print("■ 関係式")
        for i in range(call0(eq, "GetCount")):
            print("  ", eq.Equation(i))
    finally:
        sw.restore()


if __name__ == "__main__":
    main()
