"""PC ごとに違う設定。設定ファイルと環境変数で上書きできる。

優先順: 環境変数 SWKIT_* > 設定ファイル（%USERPROFILE%/.swkit.json または SWKIT_CONFIG のパス） > 既定値

    {"progid": "SldWorks.Application.32",
     "part_template": "D:/templates/部品.prtdot",
     "asm_template": "D:/templates/アセンブリ.asmdot"}

テンプレートは、SOLIDWORKS の既定テンプレート設定が無効なときだけ使う。
"""
import json
import os
from pathlib import Path

_path = Path(os.environ.get("SWKIT_CONFIG", Path.home() / ".swkit.json"))
_file = json.loads(_path.read_text(encoding="utf-8")) if _path.exists() else {}


def _get(key, default):
    return os.environ.get("SWKIT_" + key.upper(), _file.get(key, default))


# SOLIDWORKS の COM 名。版を指定するなら SldWorks.Application.32（2024）のように書く
PROGID = _get("progid", "SldWorks.Application")
PART_TEMPLATE = _get("part_template", "")
ASM_TEMPLATE = _get("asm_template", "")
# 旋削部品・回転体の中心に作る基準軸の名前
AXIS_NAME = _get("axis_name", "中心軸")
