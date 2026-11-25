"""swkit: SOLIDWORKS を Python から操作して、人が後から直せるモデルを作るための部品。

特定の製品に依らない。設計ごとのコードは、設計パラメータのモジュールと Project の派生クラスを用意して使う。
作り方の決まりは docs/MODELING_RULES.md を参照。
"""
from .core import AXES, MM, NOTHING, SW, Asm, Doc, M, Part, Sketch, call0
from .project import (BLACK, BLUE, FLIP_X, GREY, I3, ORANGE, X_DOWN, X_TO_NEG_Z, X_TO_Y, X_TO_Z, X_TO_Z_Y_TO_X, YELLOW,
                      Project, cached_part)

__all__ = ["AXES", "MM", "NOTHING", "SW", "Asm", "Doc", "M", "Part", "Sketch", "call0", "Project", "cached_part",
           "I3", "FLIP_X", "X_DOWN", "X_TO_NEG_Z", "X_TO_Y", "X_TO_Z", "X_TO_Z_Y_TO_X", "GREY", "BLACK", "YELLOW", "BLUE", "ORANGE"]

__version__ = "0.1.0"
