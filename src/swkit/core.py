"""SOLIDWORKS API(COM) の薄いラッパ。

モデルの作り方の方針
  - スケッチは拘束（水平・鉛直・同一直線・一致・等径）と駆動寸法で完全定義にする。
  - 寸法値は設計パラメータ名を使った関係式でつなぐ。部品のグローバル変数を変えれば形が追従する。
  - フィーチャは加工工程の単位で分け、工程名を付ける。
  - 角 R・面取りはフィレット／面取りフィーチャ、穴は穴ウィザードで作る。
  - 対称・等配・等ピッチの形状は 1 つ作ってミラー／円形パターン／直線パターンで展開する。
  - 相手部品と共締めする穴はアセンブリの穴フィーチャで開け、各部品へ反映させる。
単位は呼び出し側が mm、API へ渡すときに m へ換算する。
"""
from __future__ import annotations

import functools
import math
import re
import time
import types
from pathlib import Path

import pythoncom
import win32com.client as wc
from win32com.client import VARIANT

from .config import AXIS_NAME, PART_TEMPLATE, ASM_TEMPLATE, PROGID
MM = 1e-3
NOTHING = VARIANT(pythoncom.VT_DISPATCH, None)

# swDocumentTypes_e
PART, ASM = 1, 2
# swEndConditions_e
BLIND, THROUGH_ALL, MIDPLANE = 0, 1, 6

AXES = {"front": (0, 1, 2), "top": (0, 2, 1), "right": (2, 1, 0)}   # 平面 -> (u, v, 法線) の軸番号
TAP_DRILL = {"M3": 2.5, "M4": 3.3, "M5": 4.2, "M6": 5.0, "M8": 6.8, "M10": 8.5}


def _byref_int():
    return VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)


def _dbl_array(values):
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values])


def M(obj, name, *args):
    """メソッドを確実にメソッドとして呼ぶ（プロパティ取得と誤認されるのを防ぐ）。"""
    obj._FlagAsMethod(name)
    return getattr(obj, name)(*args)


def call0(obj, name):
    """引数なしメソッド/プロパティのどちらでも値を得る（遅延バインディング対策）。"""
    v = getattr(obj, name)
    return v() if isinstance(v, types.MethodType) else v


def _is0(v):
    return abs(v) < 1e-9


class Profile:
    """処理の種類ごとの所要時間。入れ子になった処理は、内側の時間を外側から引いて重複なく数える。"""

    data = {}          # 種類 -> [回数, 秒]
    _stack = []

    @classmethod
    def timed(cls, label):
        """処理の所要時間を種類ごとに積算するデコレータ．"""
        def deco(fn):
            @functools.wraps(fn)
            def wrapper(*a, **k):
                cls._stack.append(0.0)
                t0 = time.perf_counter()
                try:
                    return fn(*a, **k)
                finally:
                    dt = time.perf_counter() - t0
                    inner = cls._stack.pop()
                    rec = cls.data.setdefault(label, [0, 0.0])
                    rec[0] += 1
                    rec[1] += dt - inner
                    if cls._stack:
                        cls._stack[-1] += dt
            return wrapper
        return deco

    @classmethod
    def report(cls, total=None):
        """処理の種類ごとの回数と所要時間を表示し，辞書で返す．"""
        rows = sorted(cls.data.items(), key=lambda kv: -kv[1][1])
        known = sum(v[1] for _, v in rows)
        print(f"{'処理':<26}{'回数':>7}{'秒':>9}{'1 回 ms':>10}")
        for k, (n, t) in rows:
            print(f"{k:<26}{n:>7}{t:>9.1f}{t / n * 1000:>10.0f}")
        if total:
            print(f"{'（上記以外）':<26}{'':>7}{total - known:>9.1f}")
        return {k: {"count": n, "seconds": round(t, 2)} for k, (n, t) in rows}


timed = Profile.timed


class SW:
    # 表示モード。True にすると画面とフィーチャツリーの更新を止めずに作る（作る過程を見せる・録画する用。遅くなる）
    SHOW = False

    def __init__(self, params=None):
        """SOLIDWORKS に接続し，生成中だけ画面更新と視点のアニメーションを止める．params は設計値のモジュール．"""
        self.params = params
        self.app = wc.Dispatch(PROGID)
        self.app.Visible = True
        self._input_dim = self.app.GetUserPreferenceToggle(10)
        self.app.SetUserPreferenceToggle(10, False)   # 寸法作成時に値入力のダイアログを出さない
        self.app.CommandInProgress = not SW.SHOW      # 連続した API 呼び出しであることを伝える（処理が速くなる）
        self.math = call0(self.app, "GetMathUtility")
        self.part_tpl = self._template(8, "*.prtdot")
        self.asm_tpl = self._template(9, "*.asmdot")
        # 中間形式の取り込みなどでテンプレート選択のダイアログが出ると処理が止まる。
        # 実行中だけ「既定テンプレートを常に使う」にし、既定テンプレートが無効なら有効なものを入れる。終了時に戻す
        self._prefs = {"toggle": self.app.GetUserPreferenceToggle(111),
                       8: self.app.GetUserPreferenceStringValue(8), 9: self.app.GetUserPreferenceStringValue(9),
                       "anim": self.app.GetUserPreferenceDoubleValue(38), "mate_anim": self.app.GetUserPreferenceDoubleValue(37),
                       "normal_to": self.app.GetUserPreferenceToggle(477)}
        if not SW.SHOW:
            # 視点が動くアニメーションと、スケッチを開くたびに視点を面へ向ける動きを止める（スケッチ 1 回で約 0.8 秒かかっていた）
            self.app.SetUserPreferenceDoubleValue(38, 0.0)
            self.app.SetUserPreferenceDoubleValue(37, 0.0)
            self.app.SetUserPreferenceToggle(477, False)
        self.app.SetUserPreferenceToggle(111, True)
        self.app.SetUserPreferenceStringValue(8, self.part_tpl)
        self.app.SetUserPreferenceStringValue(9, self.asm_tpl)

    def restore(self):
        """変更した SOLIDWORKS の設定を元に戻す．終了時に必ず呼ぶ．"""
        self.app.CommandInProgress = False
        self.app.SetUserPreferenceToggle(10, self._input_dim)
        self.app.SetUserPreferenceToggle(111, self._prefs["toggle"])
        self.app.SetUserPreferenceStringValue(8, self._prefs[8])
        self.app.SetUserPreferenceStringValue(9, self._prefs[9])
        self.app.SetUserPreferenceDoubleValue(38, self._prefs["anim"])
        self.app.SetUserPreferenceDoubleValue(37, self._prefs["mate_anim"])
        self.app.SetUserPreferenceToggle(477, self._prefs["normal_to"])

    @staticmethod
    def quiet(doc, on=True):
        """文書の画面更新とフィーチャツリー更新を止める（on=False で戻す）。表示モードでは止めない。"""
        if SW.SHOW and on:
            return
        try:
            fm = doc.FeatureManager
            fm.EnableFeatureTree = not on
            fm.EnableFeatureTreeWindow = not on
            view = doc.ActiveView
            if view is not None:
                view.EnableGraphicsUpdate = not on
        except Exception:
            pass

    def _template(self, pref, pattern):
        p = self.app.GetUserPreferenceStringValue(pref)
        if p and Path(p).exists():
            return p
        fallback = PART_TEMPLATE if pattern == "*.prtdot" else ASM_TEMPLATE
        if not fallback or not Path(fallback).exists():
            raise RuntimeError(f"テンプレートが見つからない: {pattern}。SOLIDWORKS の既定テンプレートを設定するか、"
                               "~/.swkit.json の part_template / asm_template にパスを書く")
        return str(fallback)

    # ------------------------------------------------------------ 文書
    @timed("新規部品")
    def new_part(self):
        # 文書は表示したまま作る。非表示（DocumentVisible False）だとスケッチを開けない
        """新しい部品を作り，Part を返す．"""
        return Part(self, self.app.NewDocument(self.part_tpl, 0, 0, 0), self.params)

    @timed("新規アセンブリ")
    def new_asm(self):
        """新しいアセンブリを作り，Asm を返す．"""
        return Asm(self, self.app.NewDocument(self.asm_tpl, 0, 0, 0), self.params)

    @timed("文書を開く")
    def open(self, path, kind=None):
        """部品またはアセンブリを開き，文書オブジェクトを返す．"""
        path = str(path)
        if kind is None:
            kind = ASM if path.lower().endswith(".sldasm") else PART
        e, w = _byref_int(), _byref_int()
        doc = self.app.OpenDoc6(path, kind, 1, "", e, w)  # silent
        if doc is None:
            raise RuntimeError(f"開けない: {path} err={e.value}")
        return doc

    def activate(self, doc):
        """文書を前面にする．寸法へ関係式を付ける前に必要となる．"""
        e = _byref_int()
        self.app.ActivateDoc3(call0(doc, "GetTitle"), False, 2, e)

    @timed("文書を閉じる")
    def close_under(self, root):
        """root 配下に保存された文書だけを閉じる。ほかに開いている文書には触れない。"""
        root = str(Path(root).resolve()).lower()
        for _ in range(4):  # 参照元のアセンブリを先に閉じ、残った部品を次の周で閉じる
            docs = []
            for d in (call0(self.app, "GetDocuments") or ()):
                try:
                    if str(Path(call0(d, "GetPathName") or "?").resolve()).lower().startswith(root):
                        docs.append((call0(d, "GetType"), call0(d, "GetTitle")))
                except (AttributeError, pythoncom.com_error):
                    pass
            if not docs:
                return
            for _, title in sorted(docs, reverse=True):
                self.app.CloseDoc(title)

    def transform(self, rows, t_mm):
        """rows: 部品の x,y,z 軸がアセンブリ座標で向く単位ベクトル 3本。t_mm: 原点位置。"""
        a = [c for r in rows for c in r] + [t * MM for t in t_mm] + [1.0, 0, 0, 0]
        return M(self.math, "CreateTransform", _dbl_array(a))


class Sketch:
    """拘束と駆動寸法で完全定義するスケッチ。

    座標は式（文字列）か数値で与える。式に出てくる大文字の名前は設計パラメータで、文書の
    グローバル変数として登録し、寸法を関係式でその変数につなぐ。
    2D 座標 (u, v) の意味は平面で決まる: 正面 = (X, Y)、平面 = (X, Z)、右側面 = (Z, Y)。
    同じ式の座標を持つ線・点は同一直線上の拘束でまとめ、寸法は式ごとに 1 本だけ入れる。
    寸法はすべて原点（＝部品の加工基準）から測る。
    """

    @timed("スケッチ開始")
    def __init__(self, owner, base, feature, revolve=False, edit=False):
        """基準平面にスケッチを開く．Part.sketch と Asm.sketch から呼ばれる．"""
        self.o, self.doc, self.revolve = owner, owner.doc, revolve
        self.iu, self.iv, _ = AXES[base]
        doc = self.doc
        doc.ClearSelection2(True)
        feature.Select2(False, 0)
        self.sm = doc.SketchManager
        if edit:                              # 既存スケッチ（穴位置）を開く
            M(doc, "EditSketch")
        else:
            self.sm.InsertSketch(True)
        self.sm.AddToDB = True                # 推論スナップを止め、拘束は明示的に付ける
        self.sm.DisplayWhenAdded = SW.SHOW    # 表示モードでは描いた線をその場で見せる
        self.sk = call0(self.sm, "ActiveSketch")
        if self.sk is None:
            raise RuntimeError("スケッチを開けない")
        self.xf = self.sk.ModelToSketchTransform
        self.items = {0: {}, 1: {}}           # 軸 -> 式 -> [("line"|"pt", obj)]
        self.segs = []                        # 端点を結合する対象
        self.circ = {}                        # 直径の式 -> [円]
        self.rad = {}                         # 半径の式 -> [円弧]
        self.axis = None
        self.ndim = 0
        # 面に置いたスケッチは向きが決まっていない。u 軸がスケッチの縦に写るなら水平と鉛直を入れ替える
        (x0, y0), (x1, y1) = self._xy(0, 0), self._xy(1, 0)
        k = 0 if abs(x1 - x0) < abs(y1 - y0) else 1
        hv = ("sgHORIZONTAL2D", "sgVERTICAL2D")
        pp = ("sgHORIZONTALPOINTS2D", "sgVERTICALPOINTS2D")
        dm = ("AddVerticalDimension2", "AddHorizontalDimension2")
        self.LINE = {0: hv[k], 1: hv[1 - k]}          # 軸 0: u 一定の線, 軸 1: v 一定の線
        self.PTS = {0: pp[k], 1: pp[1 - k]}           # 軸 0: u が等しい 2 点
        self.DIM = {0: dm[k], 1: dm[1 - k]}           # 軸 0: u 方向の距離

    # ---------------------------------------------------------- 座標
    def val(self, e):
        """式を設計値で評価して数値 mm を返す．"""
        return self.o.val(e)

    def _xy(self, u, v):
        """(u, v) mm → スケッチ座標 m。"""
        m = [0.0, 0.0, 0.0]
        m[self.iu], m[self.iv] = u * MM, v * MM
        q = M(M(self.o.sw.math, "CreatePoint", _dbl_array(m)), "MultiplyTransform", self.xf).ArrayData
        return q[0], q[1]

    def _reg(self, axis, expr, kind, obj, other=None):
        """other: 点のもう一方の座標値。対称拘束を掛けられる相手を探すのに使う。"""
        self.items[axis].setdefault(str(expr), []).append((kind, obj, other))

    # ---------------------------------------------------------- 図形
    def centerline(self, length):
        """回転軸。原点から +u 方向。"""
        (x1, y1), (x2, y2) = self._xy(0, 0), self._xy(self.val(length), 0)
        self.axis = self.sm.CreateCenterLine(x1, y1, 0, x2, y2, 0)
        self._rel(self.LINE[1], self.axis)
        self._rel("sgCOINCIDENT", self.axis.GetStartPoint2, self.o.origin)
        self._rel("sgFIXED", self.axis.GetEndPoint2)       # 軸の長さは形状に関係しない
        return self.axis

    @timed("スケッチ 線を描く")
    def poly(self, pts, close=True, free=()):
        """折れ線。水平・鉛直の辺には拘束を付け、位置を式ごとの寸法で決める。

        free: 位置を寸法で決めない頂点の番号（つながる円弧の半径で決まる端点）。
        """
        pts = [(str(u), str(v)) for u, v in pts]
        num = [(self.val(u), self.val(v)) for u, v in pts]
        n = len(pts)
        pairs = [(i, (i + 1) % n) for i in range(n if close else n - 1)]
        covered = {0: set(), 1: set()}
        lines = {}
        for i, j in pairs:
            (x1, y1), (x2, y2) = self._xy(*num[i]), self._xy(*num[j])
            seg = lines[i] = self.sm.CreateLine(x1, y1, 0, x2, y2, 0)
            self.segs.append(seg)
            for ax in (0, 1):
                if _is0(num[i][ax] - num[j][ax]):
                    self._rel(self.LINE[ax], seg)
                    self._reg(ax, pts[i][ax], "line", seg)
                    covered[ax] |= {i, j}
                    break
        for i in range(n):                    # 斜辺だけにつながる頂点は点で位置を決める
            pt = lines[i].GetStartPoint2 if i in lines else lines[i - 1].GetEndPoint2
            for ax in (0, 1):
                if i not in covered[ax] and i not in free:
                    self._reg(ax, pts[i][ax], "pt", pt, num[i][1 - ax])
        return lines

    def rect(self, u0, u1, v0, v1):
        """辺が軸に平行な長方形．4辺の位置を式で与える．"""
        return self.poly([(u0, v0), (u1, v0), (u1, v1), (u0, v1)])

    @timed("スケッチ 円を描く")
    def circle(self, u, v, d):
        """中心 (u, v)，直径 d の円．"""
        x, y = self._xy(self.val(u), self.val(v))
        seg = self.sm.CreateCircleByRadius(x, y, 0, self.val(d) / 2 * MM)
        c = seg.GetCenterPoint2
        self._reg(0, u, "pt", c, self.val(v))
        self._reg(1, v, "pt", c, self.val(u))
        self.circ.setdefault(str(d), []).append(seg)
        return seg

    def half_circle(self, r, upper):
        """原点中心の半円弧（筒受けの受け面）。端点は同じ位置にある線の端点と結合される。"""
        rr = self.val(r)

        def at(a):
            return self._xy(rr * math.cos(math.radians(a)), rr * math.sin(math.radians(a)))
        a0, a1 = (0, 180) if upper else (180, 360)
        (x1, y1), (x2, y2), (xm, ym) = at(a0), at(a1), at((a0 + a1) / 2)
        seg = self.sm.Create3PointArc(x1, y1, 0, x2, y2, 0, xm, ym, 0)
        self.segs.append(seg)
        self._rel("sgCOINCIDENT", seg.GetCenterPoint2, self.o.origin)
        self.rad.setdefault(str(r), []).append(seg)
        return seg

    @timed("スケッチ 円弧を描く")
    def arc(self, p0, p1, r, toward, concentric=None, center=None):
        """p0 から p1 への円弧（劣弧）。半径 r は式につなぐ。

        端点は，同じ位置にある線の端点（poly で free に指定した頂点など）と結合され，位置はその線の側で決まる。
        toward: 円弧の中心がある側を示す点 (u, v)。弦のどちら側へ膨らむかを決める。
        concentric: 中心を共有する円弧（先に描いた arc の戻り値）。板厚一定の曲がり板の内外面に使う。
        center: 中心の位置を式で決める場合の (u, v)。None の成分は決めない。
        端点 2つと半径で形が決まるので，その場合は concentric も center も要らない。
        """
        (u0, v0), (u1, v1) = [(self.val(a), self.val(b)) for a, b in (p0, p1)]
        rr = self.val(r)
        mu, mv = (u0 + u1) / 2, (v0 + v1) / 2
        half = math.hypot(u1 - u0, v1 - v0) / 2
        if half >= rr or _is0(half):
            raise ValueError(f"円弧を描けない: 弦 {2 * half:.3f} mm に対して半径 {rr:.3f} mm")
        nu, nv = -(v1 - v0) / (2 * half), (u1 - u0) / (2 * half)       # 弦に垂直な単位ベクトル
        if (self.val(toward[0]) - mu) * nu + (self.val(toward[1]) - mv) * nv < 0:
            nu, nv = -nu, -nv
        h = math.sqrt(rr * rr - half * half)
        cu, cv = mu + h * nu, mv + h * nv                              # 中心
        qu, qv = cu - rr * nu, cv - rr * nv                            # 弧の中点（中心の反対側）
        (x1, y1), (x2, y2), (xm, ym) = self._xy(u0, v0), self._xy(u1, v1), self._xy(qu, qv)
        seg = self.sm.Create3PointArc(x1, y1, 0, x2, y2, 0, xm, ym, 0)
        if seg is None:
            raise RuntimeError(f"円弧を作れない: {p0} - {p1}")
        self.segs.append(seg)
        if concentric is not None:
            self._rel("sgCONCENTRIC", seg, concentric)
        if center is not None:
            c = seg.GetCenterPoint2
            if center[0] is not None:
                self._reg(0, center[0], "pt", c, cv)
            if center[1] is not None:
                self._reg(1, center[1], "pt", c, cu)
        self.rad.setdefault(str(r), []).append(seg)
        return seg

    def point(self, u, v):
        """位置決め用の点（レイアウトで構成部品を合致させる相手）。"""
        x, y = self._xy(self.val(u), self.val(v))
        pt = self.sm.CreatePoint(x, y, 0)
        self._reg(0, u, "pt", pt, self.val(v))
        self._reg(1, v, "pt", pt, self.val(u))
        return pt

    def hole_points(self, pts):
        """穴位置スケッチの点。穴ウィザードが置いた点はそのまま使い、残りを追加する。"""
        have = list(self.sk.GetSketchPoints2 or ())
        for u, v in pts:
            x, y = self._xy(self.val(u), self.val(v))
            pt = next((q for q in have if abs(q.X - x) < 1e-6 and abs(q.Y - y) < 1e-6), None)
            if pt is None:
                pt = self.sm.CreatePoint(x, y, 0)
            self._reg(0, u, "pt", pt, self.val(v))
            self._reg(1, v, "pt", pt, self.val(u))

    # ---------------------------------------------------------- 既存形状から線を取る
    def _segments(self):
        return list(call0(self.sk, "GetSketchSegments") or ())

    def _new_segments(self, before):
        """before のあとに増えたスケッチ線。COM オブジェクトは同一性で比べられないので個数で切り出す。"""
        return self._segments()[len(before):]

    @timed("スケッチ エンティティ変換")
    def convert(self, pts, construction=False):
        """エンティティ変換: モデル座標 pts (x, y, z) mm を通る既存のエッジを、このスケッチへ投影する。

        できた線は元のエッジに「エッジ上」の関係で結ばれ、寸法なしで完全定義になる。元の形状を直せば追従する。
        construction=True なら作図線にする（オフセットの元にだけ使う場合）。
        """
        edges = self.o.find_edges(pts)
        before = self._segments()
        self.sm.AddToDB = False               # 変換は選択にもとづく操作なので、直接書き込みモードを一時的に外す
        self.doc.ClearSelection2(True)
        for e in edges:
            M(e, "Select4", True, NOTHING)
        ok = M(self.sm, "SketchUseEdge3", False, False)
        self.doc.ClearSelection2(True)
        self.sm.AddToDB = True
        new = self._new_segments(before)
        if not ok or not new:
            raise RuntimeError(f"エンティティ変換ができない: {pts}")
        for g in new:
            g.ConstructionGeometry = construction
        return new

    @timed("スケッチ オフセット")
    def offset(self, segs, d, toward):
        """オフセット: 線 segs（convert の戻り値など）を距離 d だけ平行に移した線を作る。距離の寸法は式につなぐ。

        toward: 移す側を示す点 (u, v) mm。API の符号の向きに頼らず、できた線がこの点に近づいたかで向きを確かめる。
        """
        (tx, ty) = self._xy(self.val(toward[0]), self.val(toward[1]))

        def near(group):                      # 点 toward から線の中点までの最小距離
            out = []
            for g in group:
                a, b = call0(g, "GetStartPoint2"), call0(g, "GetEndPoint2")
                if a is not None and b is not None:
                    out.append(math.hypot((a.X + b.X) / 2 - tx, (a.Y + b.Y) / 2 - ty))
            return min(out)

        dist = abs(self.val(d)) * MM
        for sign in (1.0, -1.0):
            before = self._segments()
            self.sm.AddToDB = False
            self.doc.ClearSelection2(True)
            for g in segs:
                g.Select4(True, NOTHING)
            ok = M(self.sm, "SketchOffset2", sign * dist, False, True, 0, 0, True)
            self.doc.ClearSelection2(True)
            self.sm.AddToDB = True
            new = self._new_segments(before)
            if not ok or not new:
                raise RuntimeError(f"オフセットができない: {d}")
            if near(new) < near(segs) or sign < 0:
                break
            self.doc.ClearSelection2(True)    # 反対側に出た。消して逆向きに作り直す
            for g in new:
                g.Select4(True, NOTHING)
            M(self.doc, "EditDelete")
        # オフセット距離の寸法（このスケッチで最後にできた寸法）を式につなぐ
        feat = M(self.doc, "FeatureByPositionReverse", 0)
        dd, last = call0(feat, "GetFirstDisplayDimension"), None
        while dd is not None:
            last = dd
            dd = M(feat, "GetNextDisplayDimension", dd)
        if last is None:
            self.o.warn(f"オフセットの寸法が見つからない: {d}")
        else:
            self.o.link(M(last, "GetDimension2", 0), d)
        return new

    # ---------------------------------------------------------- 拘束・寸法
    def _sel(self, *objs):
        self.doc.ClearSelection2(True)
        for o in objs:
            if o is self.o.origin:             # 原点はスケッチ外部の点として名前で選ぶ
                self.doc.Extension.SelectByID2(f"Point1@{o.Name}", "EXTSKETCHPOINT", 0, 0, 0, True, 0, NOTHING, 0)
            else:
                o.Select4(True, NOTHING)

    @timed("スケッチ 拘束")
    def _rel(self, name, *objs):
        self._sel(*objs)
        self.doc.SketchAddConstraints(name)
        self.doc.ClearSelection2(True)

    @timed("スケッチ 寸法")
    def _dim(self, method, objs, loc, expr, scale=1.0):
        """寸法を入れて関係式でつなぐ。loc は寸法文字の位置 (u, v) mm。"""
        self._sel(*objs)
        x, y = self._xy(*loc)
        dd = M(self.doc, method, x, y, 0.0)
        self.doc.ClearSelection2(True)
        if dd is None:
            self.o.warn(f"寸法を作れない: {expr}")
            return
        self.o.link(M(dd, "GetDimension2", 0), expr, scale)

    def _merge_ends(self):
        """同じ位置にある線・円弧の端点を結合する。"""
        seen = {}
        for s in self.segs:
            for p in (s.GetStartPoint2, s.GetEndPoint2):
                key = (round(p.X * 1e7), round(p.Y * 1e7))
                if key in seen:
                    self._rel("sgMERGEPOINTS", seen[key], p)
                else:
                    seen[key] = p

    def _mirror_line(self, ax):
        """原点を通る中心線（作図線）。ax=0 なら u=0 の線で、左右対称の基準になる。"""
        if ax not in self._cl:
            end = (0.0, 25.0) if ax == 0 else (25.0, 0.0)
            (x1, y1), (x2, y2) = self._xy(0, 0), self._xy(*end)
            c = self.sm.CreateCenterLine(x1, y1, 0, x2, y2, 0)
            self._rel(self.LINE[ax], c)
            self._rel("sgCOINCIDENT", c.GetStartPoint2, self.o.origin)
            self._rel("sgFIXED", c.GetEndPoint2)           # 中心線の長さは形状に関係しない
            self._cl[ax] = c
        return self._cl[ax]

    @staticmethod
    def _negated(e):
        """式 e の符号を反転した書き方の候補。"""
        return {f"-{e}", f"-({e})", f"- {e}"}

    def _sym_pair(self, pos, neg):
        """対称拘束を掛けられる組を返す。線どうし、または他方の座標が等しい点どうし。"""
        if pos[0][0] == "line" and neg[0][0] == "line":
            return pos[0][1], neg[0][1]
        for k1, o1, w1 in pos:
            for k2, o2, w2 in neg:
                if k1 == k2 == "pt" and w1 is not None and w2 is not None and _is0(w1 - w2):
                    return o1, o2
        return None

    @timed("スケッチ 定義の整理")
    def define(self):
        """拘束と寸法を付けて完全定義にする．フィーチャを作る操作から自動で呼ばれる．"""
        origin = self.o.origin
        self._cl = {}
        self._merge_ends()
        for ax in (0, 1):                                    # 同じ式の線・点をまとめる
            for members in self.items[ax].values():
                members.sort(key=lambda m: m[0] != "line")   # 線を代表にする
                kind, rep, _ = members[0]
                for k2, obj, _ in members[1:]:
                    if kind == "line":
                        self._rel("sgCOLINEAR" if k2 == "line" else "sgCOINCIDENT", rep, obj)
                    else:
                        self._rel(self.PTS[ax], rep, obj)
        for ax in (0, 1):
            done = set()
            for expr, members in self.items[ax].items():
                if expr in done:
                    continue
                kind, rep, _ = members[0]
                v = self.val(expr)
                if _is0(v):
                    self._rel("sgCOINCIDENT" if kind == "line" else self.PTS[ax], rep, origin)
                    continue
                self.ndim += 1
                off = -(12 + 7 * self.ndim)
                if ax == 1 and self.revolve and self.axis is not None:
                    # 回転体は直径で指示する。寸法文字を軸の反対側に置くと直径寸法になる
                    self._dim("AddDimension2", (rep, self.axis), (off, -abs(v) - 5), expr, 2.0)
                    continue
                # 符号だけ違う式の相手があれば、中心線に対する対称拘束と全幅寸法 1 本で決める
                other = next((e for e in self.items[ax]
                              if (e in self._negated(expr) or expr in self._negated(e)) and _is0(self.val(e) + v)), None)
                pair = None
                if other is not None and other not in done and not self.revolve:
                    pos, neg = (members, self.items[ax][other]) if v > 0 else (self.items[ax][other], members)
                    pair = self._sym_pair(pos, neg)
                if pair is not None:
                    done.add(other)
                    self._rel("sgSYMMETRIC", pair[0], pair[1], self._mirror_line(ax))
                    e = expr if v > 0 else other
                    m = re.fullmatch(r"\s*([A-Z0-9_]+)\s*/\s*2\s*", e)
                    full = m.group(1) if m else f"2 * ( {e} )"
                    loc = (0.0, off) if ax == 0 else (off, 0.0)
                    self._dim(self.DIM[ax], pair, loc, full)
                elif ax == 0:
                    self._dim(self.DIM[0], (rep, origin), (v / 2, off), expr)
                else:
                    self._dim(self.DIM[1], (rep, origin), (off, v / 2), expr)
        for group in (self.circ, self.rad):
            for expr, segs in group.items():
                for s in segs[1:]:
                    self._rel("sgSAMELENGTH", segs[0], s)
                self.ndim += 1
                self._dim("AddDimension2", (segs[0],), (20 + 6 * self.ndim, 20 + 6 * self.ndim), expr)
        self.sm.AddToDB = False
        self.sm.DisplayWhenAdded = True
        st = self.sk.GetConstrainedStatus
        if st != 3:
            self.o.warn(f"スケッチが完全定義でない (status={st})")
        return self


class Doc:
    def __init__(self, sw: SW, doc, params=None):
        """文書を包み，関係式の管理を始める．"""
        if doc is None:
            raise RuntimeError("文書を作成できない")
        self.sw, self.doc = sw, doc
        self.ext = doc.Extension
        SW.quiet(doc)
        self.params = params          # 設計パラメータのモジュール
        self.globals = set()
        self.warnings = []
        self.eq = call0(doc, "GetEquationMgr")
        self.eq.AutomaticRebuild = False      # 式を足すたびの再構築を止める。保存の前に solve_equations でまとめて評価する
        self._eq_pending = False
        self._planes = None
        self._flip = {}
        self.origin = None
        f = call0(doc, "FirstFeature")
        while f is not None:
            if call0(f, "GetTypeName2") == "OriginProfileFeature":
                self.origin = f
            f = call0(f, "GetNextFeature")
        self._s = None
        self._ref = {}
        self.labels = {}              # 基準平面の式 -> 分かりやすい名前

    def warn(self, msg):
        """警告を記録して表示する．"""
        self.warnings.append(msg)
        print("      !", msg)

    @timed("関係式の評価")
    def solve_equations(self):
        """ためておいた関係式をまとめて評価する。"""
        if self._eq_pending:
            M(self.eq, "EvaluateAll")
            M(self.doc, "EditRebuild3")
            self._eq_pending = False

    @timed("保存")
    def save_as(self, path):
        """関係式を評価してから名前を付けて保存する．"""
        self.solve_equations()
        SW.quiet(self.doc, False)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        e, w = _byref_int(), _byref_int()
        ok = self.ext.SaveAs(str(path), 0, 1, NOTHING, e, w)  # silent
        if not ok:
            raise RuntimeError(f"保存失敗: {path} err={e.value} warn={w.value}")
        self.path = path
        return path

    @timed("保存")
    def save(self):
        """関係式を評価してから上書き保存する．"""
        self.solve_equations()
        e, w = _byref_int(), _byref_int()
        if not self.doc.Save3(1, e, w):
            raise RuntimeError(f"保存失敗 err={e.value}")

    @timed("再構築")
    def rebuild(self):
        """関係式を評価して再構築する．"""
        self.solve_equations()
        self.doc.ForceRebuild3(False)

    def set_prop(self, name, value):
        """ユーザー定義プロパティを設定する．"""
        cpm = self.ext.CustomPropertyManager("")
        cpm.Add3(name, 30, str(value), 2)  # text, 既存は上書き

    @timed("書き出し")
    def export(self, path):
        """STEP などの形式で書き出す．形式は拡張子で決まる．"""
        e, w = _byref_int(), _byref_int()
        return self.ext.SaveAs(str(path), 0, 1, NOTHING, e, w)

    def section(self, base="front", on=True, reverse=False):
        """基準平面 base（front / top / right）で切った断面表示にする。on=False で元に戻す。

        内部が外から見えない組立品の画像を書き出すときに使う。モデルは変更しない。
        """
        mvm = self.doc.ModelViewManager
        if not on:
            M(mvm, "RemoveSectionView")
            return True
        data = call0(mvm, "CreateSectionViewData")
        data.FirstPlane = self.planes()[base]
        data.FirstReverseDirection = bool(reverse)
        data.FirstOffset = 0.0
        ok = bool(M(mvm, "CreateSectionView", data))
        if not ok:
            self.warn(f"断面表示にできない: {base}")
        return ok

    def image(self, path, view=7):
        """形だけの画像を書き出す（スケッチ・平面・軸・原点は隠す）。view: 7 = 等角、1 = 正面、5 = 上面、4 = 右側面。"""
        self.sw.activate(self.doc)
        SW.quiet(self.doc, False)
        try:
            self.sw.app.FrameState = 1                   # ウィンドウを最大化
            self.ext.SetUserPreferenceToggle(198, 0, True)
        except Exception:
            pass
        self.doc.ShowNamedView2("", view)
        M(self.doc, "ViewZoomtofit2")
        ok = self.export(path)
        try:
            self.ext.SetUserPreferenceToggle(198, 0, False)
        except Exception:
            pass
        return ok

    def planes(self):
        """正面・平面・右側面（言語に依らず先頭 3 つの参照平面）。1 回探したら覚えておく。"""
        if self._planes is None:
            out, f = [], call0(self.doc, "FirstFeature")
            while f is not None and len(out) < 3:
                if call0(f, "GetTypeName2") == "RefPlane":
                    out.append(f)
                f = call0(f, "GetNextFeature")
            self._planes = dict(zip(("front", "top", "right"), out))
        return self._planes

    def plane_names(self):
        """正面・平面・右側面の名前を返す．"""
        return {k: f.Name for k, f in self.planes().items()}

    @timed("基準平面")
    def ref_plane(self, j, expr):
        """軸 j（0 = X, 1 = Y, 2 = Z）に垂直で、座標が expr の基準平面。0 なら既定の平面を返す。

        オフセットは式につなぐ。向きは API の既定に任せず、作った平面の位置を確かめて決める。
        """
        base = ("right", "top", "front")[j]
        v = self.val(expr)
        if _is0(v):
            return self.planes()[base]
        key = (j, str(expr))
        if key in self._ref:
            return self._ref[key]
        # 既定の平面からのオフセットは、反転なしで + 側、反転ありで − 側へ出る。
        # 作った平面の位置は平面自身の変換行列から読んで確かめる（確認用のスケッチは開かない）。
        # テンプレートの平面の向きが標準と違って逆側に出た場合だけ、消して反対向きに作り直す
        want = self._flip.get(base, False) != (v < 0)
        for flip in (want, not want):
            self.doc.ClearSelection2(True)
            self.planes()[base].Select2(False, 0)
            f = self.doc.FeatureManager.InsertRefPlane(8 | (256 if flip else 0), abs(v) * MM, 0, 0, 0, 0)
            if f is None:
                raise RuntimeError(f"基準平面を作れない: {expr}")
            self.doc.ClearSelection2(True)
            pos = call0(f, "GetSpecificFeature2").Transform.ArrayData[9 + j]
            if abs(pos - v * MM) < 1e-7:
                self._flip[base] = flip != (v < 0)     # True = この文書の平面は標準と逆向き
                f.Name = self.labels.get(str(expr)) or f"{'XYZ'[j]}面 {expr}".replace(' / ', '÷').replace('=', ' ')
                self.link_feature(f, expr)
                self._ref[key] = f
                return f
            f.Select2(False, 0)
            M(self.doc, "EditDelete")
        raise RuntimeError(f"基準平面の向きを決められない: {expr}")

    @staticmethod
    def planes_of(doc):
        """文書の正面・平面・右側面の名前。"""
        out, f = [], call0(doc, "FirstFeature")
        while f is not None and len(out) < 3:
            if call0(f, "GetTypeName2") == "RefPlane":
                out.append(f.Name)
            f = call0(f, "GetNextFeature")
        return dict(zip(("front", "top", "right"), out))

    def title(self):
        """文書の題名を返す．"""
        return call0(self.doc, "GetTitle")

    @timed("質量特性")
    def mass(self):
        """(質量 kg, 体積 m3, 重心 mm)。"""
        mp = self.ext.CreateMassProperty
        mp.UseSystemUnits = True
        c = mp.CenterOfMass
        return mp.Mass, mp.Volume, tuple(v / MM for v in c)

    # ------------------------------------------------------------ 式
    def val(self, e):
        """式を設計値で評価して数値 mm を返す．"""
        if isinstance(e, (int, float)):
            return float(e)
        return float(eval(e, {"__builtins__": {}}, vars(self.params)))

    def _eq_text(self, expr):
        """式中のパラメータ名をグローバル変数として登録し、SOLIDWORKS の式に直す。"""
        def rep(m):
            name = m.group(0)
            if name not in self.globals:
                M(self.eq, "Add2", -1, f'"{name}" = {getattr(self.params, name):g}', False)
                self._eq_pending = True
                self.globals.add(name)
            return f'"{name}"'
        return re.sub(r"[A-Z][A-Z0-9_]*", rep, str(expr))

    @timed("関係式")
    def link(self, dim, expr, scale=1.0):
        """寸法 dim を式 expr につなぐ。scale=2 は半径の式を直径寸法へつなぐ場合。"""
        if isinstance(expr, (int, float)) or not re.search(r"[A-Z]", str(expr)):
            return                                   # 変数を含まない寸法は値のまま
        want = abs(self.val(expr))
        got = dim.SystemValue / MM
        if scale == 2.0 and abs(got - 2 * want) < 1e-3:
            m = re.fullmatch(r"\s*([A-Z0-9_]+)\s*/\s*2\s*", str(expr))
            text = self._eq_text(m.group(1)) if m else f"2 * ( {self._eq_text(expr)} )"
        elif abs(got - want) < 1e-3:
            e = str(expr).strip()
            if self.val(expr) >= 0:
                text = self._eq_text(e)
            elif e.startswith("-(") and e.endswith(")") and e.count("(") == 1:
                text = self._eq_text(e[2:-1])                # -(A + B) の寸法は A + B
            elif e.startswith("-") and re.fullmatch(r"[A-Z0-9_ ./*]+", e[1:]):
                text = self._eq_text(e[1:].strip())          # -A / 2 の寸法は A / 2
            else:
                text = f"abs( {self._eq_text(e)} )"       # 寸法は正の値で持つ
        else:
            self.warn(f"寸法値 {got:g} が式 {expr} = {want:g} と合わない")
            return
        name = "@".join(dim.FullName.split("@")[:2])
        if M(self.eq, "Add2", -1, f'"{name}" = {text}', False) < 0:
            # 別の文書（取り込んだメーカーデータなど）がアクティブだと寸法の式は断られる。自分をアクティブにしてやり直す
            self.sw.activate(self.doc)
            self.solve_equations()
            if M(self.eq, "Add2", -1, f'"{name}" = {text}', True) < 0:
                have = [self.eq.Equation(i) for i in range(self.eq.GetCount)]
                self.warn(f"関係式を追加できない: {name} = {text}  （寸法の正式名 {dim.FullName}、既にある式 {have}）")
        self._eq_pending = True

    def link_feature(self, feat, expr):
        """フィーチャの寸法（板厚・フィレット半径など）を式につなぐ。"""
        dd = call0(feat, "GetFirstDisplayDimension")
        if dd is not None:
            self.link(M(dd, "GetDimension2", 0), expr)

    # ------------------------------------------------------------ 穴（穴ウィザード）
    @timed("穴ウィザード")
    def hole(self, base, w, pts, size, name, from_pos=True, depth=None, thread=None, scope=None):
        """穴ウィザードで JIS のキリ穴（size="Ø5.5"）またはタップ穴（size="M4"）をあける。

        base: 穴をあける面の向き（front / top / right に平行な面）。w: その面の法線方向の位置。
        pts : 面内の穴位置 [(u, v)]。位置スケッチは原点基準の寸法と拘束で完全定義にする。
        from_pos: 法線の + 側から工具を入れるなら True。depth=None は貫通。
        scope: アセンブリで穴をあける構成部品。指定した部品だけを貫き、各部品へ反映される。
        """
        iu, iv, iw = AXES[base]
        sgn = 1.0 if from_pos else -1.0
        o, d = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
        o[iu], o[iv] = self.val(pts[0][0]) * MM, self.val(pts[0][1]) * MM
        o[iw] = self.val(w) * MM + sgn * 0.002   # 面のすぐ外側 2 mm から面へ向けて位置を指示する
        d[iw] = -sgn
        self.doc.ClearSelection2(True)
        if not M(self.ext, "SelectByRay", o[0], o[1], o[2], d[0], d[1], d[2], 0.0002, 2, True, 0, 0):
            raise RuntimeError(f"穴をあける面が見つからない: {name}")
        asm = scope is not None
        for c in scope or ():
            M(c, "Select4", True, call0(self.doc.SelectionManager, "CreateSelectData"), False)
        a118 = math.radians(118)
        tail = ("", False, True, True, True, not asm, asm)
        dep = (self.val(depth) if depth is not None else 10.0) * MM
        fm = self.doc.FeatureManager
        if size.startswith("M"):
            th = (self.val(thread) if thread is not None else self.val(depth) - 2) * MM
            f = M(fm, "HoleWizard5", 4, 9, 164, size, 0, TAP_DRILL[size] * MM, dep, -1.0,
                  th, -1.0, -1.0, -1.0, -1.0, a118, 1.0, -1.0, -1.0, -1.0, -1.0, -1.0, *tail)
        else:
            f = M(fm, "HoleWizard5", 2, 9, 160, size, 0, float(size.lstrip("Ø")) * MM, dep, -1.0,
                  -1.0, a118, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0, *tail)
        if f is None:
            raise RuntimeError(f"穴ウィザードに失敗: {name} {size}")
        if depth is None:                        # 貫通は作成後に押し出し状態を変える（部品の場合）
            dfn = call0(f, "GetDefinition")
            dfn.EndCondition = THROUGH_ALL
            if not M(f, "ModifyDefinition", dfn, self.doc, NOTHING):
                self.warn(f"貫通に変更できない: {name}")
        sk = Sketch(self, base, call0(f, "GetFirstSubFeature"), edit=True)
        sk.hole_points(pts)
        sk.define()
        self.doc.SketchManager.InsertSketch(True)
        self.doc.ClearSelection2(True)
        f.Name = name
        return f


class Part(Doc):
    """加工工程ごとにフィーチャを分けて作る部品。寸法は設計パラメータの関係式で駆動する。"""

    def __init__(self, sw, doc, params=None):
        """部品の文書を包む．"""
        super().__init__(sw, doc, params)
        self._axis = None

    def set_material(self, name, db="SOLIDWORKS Materials"):
        """SOLIDWORKS の材料を割り当てる．"""
        self.doc.SetMaterialPropertyName2("", db, name)

    def set_density(self, rho):
        """密度 kg/m3 を設定する．

        テンプレートに材料（「未指定」など）が割り当ててあると，部品単体では設定した密度で計算されるのに，
        アセンブリの質量にはその材料の密度が使われる．先に材料の割り当てを外す．
        """
        self.doc.SetMaterialPropertyName2("", "", "")
        self.ext.SetUserPreferenceDouble(7, 0, float(rho))   # swMaterialPropertyDensity

    def set_color(self, rgb, transparency=0.0):
        """色と透明度を設定する．"""
        r, g, b = rgb
        self.doc.MaterialPropertyValues = _dbl_array((r, g, b, 1, 1, 0.3, 0.3, transparency, 0))

    # ------------------------------------------------------------ スケッチ → フィーチャ
    def sketch(self, base, revolve=False):
        """base: front / top / right。スケッチ平面は部品の加工基準に合わせた基準平面を使う。"""
        self._s = Sketch(self, base, self.planes()[base], revolve)
        return self._s

    @timed("押し出し")
    def extrude_mid(self, t, name):
        """スケッチ面を中心に両側へ合計 t（板厚、またはほぞの厚さ）。"""
        self._s.define()
        f = self.doc.FeatureManager.FeatureExtrusion3(
            True, False, False, MIDPLANE, 0, self.val(t) * MM, 0, False, False, False, False,
            0, 0, False, False, False, False, True, True, True, 0, 0, False)
        f = self._done(f, name, "押し出し")
        self.link_feature(f, t)
        return f

    @timed("回転")
    def revolve(self, name, cut=False):
        """スケッチ内の中心線まわりに 360°（旋削）。"""
        self._s.define()
        f = self.doc.FeatureManager.FeatureRevolve2(
            True, True, False, cut, False, False, 0, 0, 2 * math.pi, 0,
            False, False, 0, 0, 0, 0, 0, True, True, True)
        return self._done(f, name, "回転")

    @timed("カット")
    def cut_through(self, name):
        """スケッチ面の両側へ全貫通（窓・角穴の切り抜き）。"""
        self._s.define()
        f = self.doc.FeatureManager.FeatureCut4(
            False, False, False, THROUGH_ALL, THROUGH_ALL, 0, 0, False, False, False, False,
            0, 0, False, False, False, False, False, True, True, True, True, False,
            0, 0, False, False)
        return self._done(f, name, "カット")

    @timed("板金")
    def base_flange(self, width, t, r, name):
        """板金のベースフランジ。開いた輪郭（曲げ断面）をスケッチ面の両側へ合計 width 押し出す。

        スケッチの線が板の外面になり、板厚は曲げの内側へ付く。曲げ半径 r は内 R。展開図も自動で付く。
        """
        self._s.define()
        self.doc.SketchManager.InsertSketch(True)
        fm = self.doc.FeatureManager
        cba = call0(fm, "CreateCustomBendAllowance")
        cba.KFactor = 0.5
        f = M(fm, "InsertSheetMetalBaseFlange2", self.val(t) * MM, False, self.val(r) * MM, self.val(width) * MM, 0.0,
              False, MIDPLANE, 0, 1, cba, False, 0, 0.0001, 0.0001, 0.5, True, False, True, True)
        return self._done(f, name, "板金ベースフランジ")

    def _face_at(self, base, w, at, from_pos=True):
        """base に平行な面のうち、法線方向の位置 w、面内の点 at を通るものを選択する。追加選択にはしない。"""
        iu, iv, iw = AXES[base]
        sgn = 1.0 if from_pos else -1.0
        o, d = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
        o[iu], o[iv] = self.val(at[0]) * MM, self.val(at[1]) * MM
        o[iw] = self.val(w) * MM + sgn * 0.002
        d[iw] = -sgn
        self.doc.ClearSelection2(True)
        if not M(self.ext, "SelectByRay", o[0], o[1], o[2], d[0], d[1], d[2], 0.0002, 2, False, 0, 0):
            raise RuntimeError(f"面が見つからない: {base} {w} {at}")
        return M(self.doc.SelectionManager, "GetSelectedObject6", 1, -1)

    def sketch_face(self, base, w, at=(0, 0), from_pos=True):
        """部品の平らな面にスケッチを開く。base: 面の向き（front / top / right）、w: 法線方向の位置、at: 面内の 1 点。

        基準平面ではなく面に描く必要があるフィーチャ（板金のスケッチ曲げ）に使う。座標の意味は sketch と同じ。
        """
        self._s = Sketch(self, base, self._face_at(base, w, at, from_pos))
        self._s.face = (base, w, from_pos)
        return self._s

    @timed("板金")
    def sketched_bend(self, angle, r, fixed, name, flip=False):
        """板金の平らな面に描いた 1 本の線で板を折る（スケッチ曲げ）。平板を抜いてから曲げる工程をそのまま表す。

        直前に sketch_face で面に曲げ線を 1 本描いておく。angle: 曲げ角 度（平らな状態からの角度）、r: 内 R、
        fixed: 動かさない側の面内の点 (u, v)。flip=True で反対向きに折る。
        """
        base, w, from_pos = self._s.face
        self._s.define()
        iu, iv, iw = AXES[base]
        sgn = 1.0 if from_pos else -1.0
        o, d = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
        o[iu], o[iv] = self.val(fixed[0]) * MM, self.val(fixed[1]) * MM
        o[iw] = self.val(w) * MM + sgn * 0.002
        d[iw] = -sgn
        self.doc.ClearSelection2(True)
        if not M(self.ext, "SelectByRay", o[0], o[1], o[2], d[0], d[1], d[2], 0.0002, 2, False, 0, 0):
            raise RuntimeError(f"固定する面が見つからない: {name}")
        fm = self.doc.FeatureManager
        cba = call0(fm, "CreateCustomBendAllowance")
        cba.KFactor = 0.5
        f = M(fm, "InsertSheetMetal3dBend", math.radians(self.val(angle)), False, self.val(r) * MM, flip, 0, cba)
        return self._done(f, name, "板金スケッチ曲げ")

    @timed("ケーブル")
    def cable(self, pts, d, name):
        """点列 pts（モデル座標 mm）を通る 3D スプラインに沿った丸断面のケーブル。ほかのボディとは結合しない。"""
        sm = self.doc.SketchManager
        sm.Insert3DSketch(True)
        arr = VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [c * MM for pt in pts for c in pt])
        sp = M(sm, "CreateSpline2", arr, False)
        if sp is None:
            sm.Insert3DSketch(True)
            raise RuntimeError(f"スプラインを作れない: {name}")
        self.doc.ClearSelection2(True)
        sp.Select4(False, NOTHING)
        self.doc.SketchAddConstraints("sgFIXED")         # 経路は座標で決める（寸法では持たない）
        sm.Insert3DSketch(True)
        path, f = None, call0(self.doc, "FirstFeature")
        while f is not None:
            if call0(f, "GetTypeName2") == "3DProfileFeature":
                path = f
            f = call0(f, "GetNextFeature")
        path.Name = f"{name} 経路"
        self.doc.ClearSelection2(True)
        path.Select2(False, 4)
        f = M(self.doc.FeatureManager, "InsertProtrusionSwept4", False, False, 0, False, False, 0, 0, False, 0.0, 0.0, 0, 0,
              False, True, True, 0.0, True, True, self.val(d) * MM, 0)
        return self._done(f, name, "ケーブル")

    def _done(self, f, name, kind):
        if f is None:
            self.doc.SketchManager.InsertSketch(True)
            raise RuntimeError(f"{kind}に失敗: {name}")
        f.Name = name
        self.doc.ClearSelection2(True)
        if SW.SHOW:                           # フィーチャが 1 つできるたびに全体を映す
            M(self.doc, "ViewZoomtofit2")
        return f

    def keep_sketch(self, name):
        """フィーチャにせずスケッチのまま残す（レイアウト用）。"""
        self._s.define()
        feat = M(self.doc, "FeatureByPositionReverse", 0)     # 開いているスケッチ（いちばん新しいフィーチャ）
        self.doc.SketchManager.InsertSketch(True)
        self.doc.ClearSelection2(True)
        if feat is not None and call0(feat, "GetTypeName2") in ("ProfileFeature", "3DProfileFeature"):
            feat.Name = name

    # ------------------------------------------------------------ フィレット・面取り
    @timed("エッジ探索")
    def find_edges(self, pts):
        """モデル座標 (x, y, z) mm の点を通るエッジを返す。表示の向きに左右されないよう形状から探す。"""
        edges = []
        for body in (M(self.doc, "GetBodies2", 0, False) or ()):
            for e in (call0(body, "GetEdges") or ()):
                sv, ev = call0(e, "GetStartVertex"), call0(e, "GetEndVertex")
                ends = (call0(sv, "GetPoint"), call0(ev, "GetPoint")) if sv is not None and ev is not None else None
                edges.append((e, ends))
        out = []
        for pt in pts:
            q = [self.val(c) * MM for c in pt]
            hit = None
            for e, ends in edges:
                if ends is not None:                 # 端点のあるエッジは弦までの距離で先に絞る
                    a, b = ends
                    ab = [b[i] - a[i] for i in range(3)]
                    t = sum((q[i] - a[i]) * ab[i] for i in range(3)) / max(sum(c * c for c in ab), 1e-18)
                    t = min(1.0, max(0.0, t))
                    if math.dist(q, [a[i] + t * ab[i] for i in range(3)]) > 1e-6:
                        continue
                cp = M(e, "GetClosestPointOn", q[0], q[1], q[2])
                if math.dist(q, cp[:3]) < 1e-6:
                    hit = e
                    break
            if hit is None:
                raise RuntimeError(f"エッジが見つからない: {pt}")
            out.append(hit)
        return out

    def _select_edges(self, pts, mark):
        edges = self.find_edges(pts)
        self.doc.ClearSelection2(True)
        for e in edges:
            sd = call0(self.doc.SelectionManager, "CreateSelectData")
            sd.Mark = mark
            M(e, "Select4", True, sd)

    @timed("フィレット")
    def fillet(self, pts, r, name):
        """pts を通るエッジに半径 r のフィレット。半径は式につなぐ。"""
        self._select_edges(pts, 1)
        e = VARIANT(pythoncom.VT_EMPTY, None)
        f = M(self.doc.FeatureManager, "FeatureFillet3", 195, self.val(r) * MM, 0.0, 0.0, 0, 0, 0, e, e, e, e, e, e, e)
        f = self._done(f, name, "フィレット")
        self.link_feature(f, r)
        return f

    @timed("面取り")
    def chamfer(self, pts, c, name):
        """pts を通るエッジに C 面取り（45°）。"""
        self._select_edges(pts, 0)
        f = M(self.doc.FeatureManager, "InsertFeatureChamfer", 4, 1, self.val(c) * MM, math.radians(45), 0.0, 0.0, 0.0, 0.0)
        f = self._done(f, name, "面取り")
        self.link_feature(f, c)
        return f

    # ------------------------------------------------------------ ミラー・パターン
    def axis_x(self):
        """X 軸（正面∩平面）の基準軸。旋削部品の中心軸で、パターンの基準に使う。"""
        if self._axis is None:
            pl = self.planes()
            self.doc.ClearSelection2(True)
            pl["front"].Select2(False, 0)
            pl["top"].Select2(True, 0)
            self.doc.InsertAxis2(True)
            f = call0(self.doc, "FirstFeature")
            while f is not None:
                if call0(f, "GetTypeName2") == "RefAxis":
                    self._axis = f
                f = call0(f, "GetNextFeature")
            self._axis.Name = AXIS_NAME
        return self._axis

    @timed("ミラー")
    def mirror(self, feats, plane, name):
        """フィーチャを基準平面 plane（front / top / right）で対称に写す。"""
        self.doc.ClearSelection2(True)
        self.planes()[plane].Select2(False, 2)
        for f in feats:
            f.Select2(True, 1)
        f = M(self.doc.FeatureManager, "InsertMirrorFeature2", False, False, False, False, 0)
        return self._done(f, name, "ミラー")

    @timed("円形パターン")
    def circular(self, feats, n, name):
        """フィーチャを中心軸まわりに等配 n 個。"""
        ax = self.axis_x()
        self.doc.ClearSelection2(True)
        for f in feats:
            f.Select2(True, 4)
        ax.Select2(True, 1)
        f = M(self.doc.FeatureManager, "FeatureCircularPattern5", int(n), 2 * math.pi, False, "NULL",
              False, True, False, False, False, False, 1, 0, "NULL", False)
        return self._done(f, name, "円形パターン")

    @timed("直線パターン")
    def linear(self, feats, pitch, expect_x, name):
        """カットフィーチャを中心軸（X）方向へピッチ pitch で 2 個に並べる。

        向きは API の既定に任せず、削られた体積の重心が expect_x(mm) に来たことで確かめる。
        """
        ax = self.axis_x()
        n = 2
        _, v0, c0 = self.mass()
        for flip in (False, True):
            self.doc.ClearSelection2(True)
            for f in feats:
                f.Select2(True, 4)
            ax.Select2(True, 1)
            f = M(self.doc.FeatureManager, "FeatureLinearPattern5", int(n), self.val(pitch) * MM, 1, 0.0,
                  flip, False, "NULL", "NULL", False, False, False, False, False, False, False, False,
                  False, False, 0.0, 0.0, False, False)
            _, v1, c1 = self.mass()
            if f is not None and abs(v0 - v1) > 1e-12 and \
                    abs((v0 * c0[0] - v1 * c1[0]) / (v0 - v1) - self.val(expect_x)) < 0.5:
                f = self._done(f, name, "直線パターン")
                self.link_feature(f, pitch)
                return f
            if f is not None:                    # 材料の外へ並んだので向きを変えてやり直す
                self.doc.ClearSelection2(True)
                f.Select2(False, 0)
                M(self.doc, "EditDelete")
        raise RuntimeError(f"直線パターンに失敗: {name}")


class Asm(Doc):
    """構成部品はレイアウトスケッチと基準平面に合致で拘束する。固定は使わない。"""

    COINCIDENT, PARALLEL = 0, 3

    def __init__(self, sw, doc, params=None):
        """アセンブリの文書を包む．"""
        super().__init__(sw, doc, params)
        self._pn = {}                      # 構成部品名 -> その文書の基準平面名
        self._axis = None
        self._axes = {}                    # 軸番号 -> 基準軸（Y, Z。X は _axis）

    def stem(self):
        """拡張子を除いた文書名を返す．"""
        return Path(self.title()).stem

    @timed("構成部品の挿入")
    def insert(self, path, rows=((1, 0, 0), (0, 1, 0), (0, 0, 1)), t=(0, 0, 0)):
        """構成部品を姿勢 rows・位置 t(mm) の近くに置く。拘束は合致で別に付ける。"""
        path = str(path)
        doc = self.sw.open(path)           # メモリに読み込まれている必要がある
        self.sw.activate(self.doc)
        comp = self.doc.AddComponent5(path, 0, "", False, "", t[0] * MM, t[1] * MM, t[2] * MM)
        if comp is None:
            raise RuntimeError(f"挿入失敗: {path}")
        if comp.IsFixed:                   # 最初の構成部品は自動で固定されるので外す
            comp.Select4(False, NOTHING, False)
            M(self.doc, "UnfixComponent")
        comp.Transform2 = self.sw.transform(rows, t)
        self.doc.ClearSelection2(True)
        self._pn[comp.Name2] = Doc.planes_of(doc)
        return comp

    @staticmethod
    def _mark(sel_mgr, mark=1):
        sd = call0(sel_mgr, "CreateSelectData")
        sd.Mark = mark
        return sd

    def _pick(self, e):
        """合致の相手を選択に加える。e は (構成部品, "front|top|right|axis") か、フィーチャ・スケッチ要素。"""
        if isinstance(e, tuple):
            comp, what = e
            if what == "axis":
                name, kind = AXIS_NAME, "AXIS"
            else:
                name, kind = self._pn[comp.Name2][what], "PLANE"
            ok = self.ext.SelectByID2(f"{name}@{comp.Name2}@{self.stem()}", kind, 0, 0, 0, True, 1, NOTHING, 0)
        else:
            try:
                ok = e.Select2(True, 1)                                  # フィーチャ（基準平面・軸）
            except Exception:
                ok = M(e, "Select4", True, self._mark(self.doc.SelectionManager))   # スケッチの線・点
        if not ok:
            raise RuntimeError(f"合致の相手を選べない: {e}")

    @timed("合致")
    def mate(self, kind, e1, e2):
        """2 つの要素を合致させる。向きは現在の姿勢に近い側を採る。"""
        self.doc.ClearSelection2(True)
        self._pick(e1)
        self._pick(e2)
        err = _byref_int()
        m = M(self.doc, "AddMate5", kind, 2, False, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, False, False, 0, err)
        self.doc.ClearSelection2(True)
        if m is None:
            raise RuntimeError(f"合致に失敗 (err={err.value}): {e1} / {e2}")
        return m

    def place(self, path, rows=((1, 0, 0), (0, 1, 0), (0, 0, 1)), t=(0, 0, 0)):
        """軸に平行な姿勢の構成部品を、基準平面 3 枚との一致で拘束する。

        t は式で与える。部品の 3 つの基準平面それぞれを、その面が重なる位置のアセンブリ基準平面
        （無ければ作る）に一致させる。
        """
        comp = self.insert(path, rows, [self.val(c) for c in t])
        for i, what in enumerate(("right", "top", "front")):     # 部品の X, Y, Z 軸に垂直な面
            j = max(range(3), key=lambda k: abs(rows[i][k]))
            if abs(abs(rows[i][j]) - 1) > 1e-9:
                raise RuntimeError("place は軸に平行な姿勢だけを扱う")
            self.mate(self.COINCIDENT, (comp, what), self.ref_plane(j, t[j]))
        if SW.SHOW:
            M(self.doc, "ViewZoomtofit2")
        return comp

    def sketch(self, base, plane=None):
        """アセンブリのレイアウトスケッチ。plane を省くと既定の平面に描く。"""
        self.sw.activate(self.doc)
        self._s = Sketch(self, base, plane or self.planes()[base])
        return self._s

    def keep_sketch(self, name):
        """スケッチを閉じ，名前を付けてレイアウトスケッチとして残す．"""
        self._s.define()
        feat = M(self.doc, "FeatureByPositionReverse", 0)     # 開いているスケッチ（いちばん新しいフィーチャ）
        self.doc.SketchManager.InsertSketch(True)
        self.doc.ClearSelection2(True)
        if feat is not None and call0(feat, "GetTypeName2") in ("ProfileFeature", "3DProfileFeature"):
            feat.Name = name

    def axis_x(self):
        """X 軸（正面∩平面）の基準軸。構成部品の円形パターンの中心。"""
        if self._axis is None:
            pl = self.planes()
            self.doc.ClearSelection2(True)
            pl["front"].Select2(False, 0)
            pl["top"].Select2(True, 0)
            self.doc.InsertAxis2(True)
            f = call0(self.doc, "FirstFeature")
            while f is not None:
                if call0(f, "GetTypeName2") == "RefAxis":
                    self._axis = f
                f = call0(f, "GetNextFeature")
            self._axis.Name = AXIS_NAME
        return self._axis

    @timed("構成部品パターン")
    def circular(self, comp, n, name):
        """構成部品を中心軸まわりに等配 n 個（構成部品の円形パターン）。"""
        ax = self.axis_x()
        self.doc.ClearSelection2(True)
        ax.Select2(False, 2)
        M(comp, "Select4", True, self._mark(self.doc.SelectionManager, 1), False)
        f = M(self.doc.FeatureManager, "FeatureCircularPattern5", int(n), 2 * math.pi, False, "NULL",
              False, True, False, False, False, False, 1, 0, "NULL", False)
        self.doc.ClearSelection2(True)
        if f is None:
            raise RuntimeError(f"構成部品の円形パターンに失敗: {name}")
        f.Name = name
        return f

    def axis_of(self, axis="x"):
        """基準平面 2 枚の交線の基準軸（x / y / z）。構成部品パターンの方向に使う。X 軸は axis_x と同じ軸。"""
        j = "xyz".index(axis)
        if j == 0:
            return self.axis_x()
        if j not in self._axes:
            pl = self.planes()
            a, b = (("front", "right"), ("top", "right"))[j - 1]     # Y 軸 = 正面∩右側面、Z 軸 = 平面∩右側面
            self.doc.ClearSelection2(True)
            pl[a].Select2(False, 0)
            pl[b].Select2(True, 0)
            self.doc.InsertAxis2(True)
            found, f = None, call0(self.doc, "FirstFeature")
            while f is not None:
                if call0(f, "GetTypeName2") == "RefAxis":
                    found = f
                f = call0(f, "GetNextFeature")
            found.Name = f"{AXIS_NAME} {axis.upper()}"
            self.doc.ClearSelection2(True)
            self._axes[j] = found
        return self._axes[j]

    @timed("構成部品パターン")
    def linear(self, comp, n, pitch, name, axis="x"):
        """構成部品を軸 axis（x / y / z）の + 方向へピッチ pitch で n 個に並べる（構成部品の直線パターン）。

        comp は構成部品 1 個か，そのリスト。リストを渡すと，組になった部品（台と，それに付くねじなど）をまとめて並べる。
        積層した同じ部品（座金，スペーサ，圧電素子など）や，等ピッチで並ぶユニットに使う。ピッチは式につなぐ。
        向きは API の既定に任せず，並んだ構成部品の位置で確かめる。
        """
        comps = list(comp) if isinstance(comp, (list, tuple)) else [comp]
        j = "xyz".index(axis)
        ax = self.axis_of(axis)
        root = M(self.doc.ConfigurationManager.ActiveConfiguration, "GetRootComponent3", True)

        def xs():
            return {c.Name2: c.Transform2.ArrayData[9 + j] for c in (call0(root, "GetChildren") or ())}
        before = xs()
        x0 = min(before[c.Name2] for c in comps)
        for flip in (False, True):
            self.doc.ClearSelection2(True)
            ax.Select2(False, 2)
            for c in comps:
                M(c, "Select4", True, self._mark(self.doc.SelectionManager, 1), False)
            f = M(self.doc.FeatureManager, "FeatureLinearPattern5", int(n), self.val(pitch) * MM, 1, 0.0,
                  flip, False, "NULL", "NULL", False, False, False, False, False, False, False, False,
                  False, False, 0.0, 0.0, False, False)
            self.doc.ClearSelection2(True)
            if f is None:
                continue
            new = [x for k, x in xs().items() if k not in before]
            if len(new) == (int(n) - 1) * len(comps) and all(x > x0 for x in new):
                f.Name = name
                dd = call0(f, "GetFirstDisplayDimension")
                while dd is not None:                  # 個数の寸法もあるので，値がピッチに等しい寸法を選ぶ
                    dim = M(dd, "GetDimension2", 0)
                    if abs(dim.SystemValue - self.val(pitch) * MM) < 1e-9 and abs(dim.SystemValue - int(n)) > 1e-9:
                        self.link(dim, pitch)
                        break
                    dd = M(f, "GetNextDisplayDimension", dd)
                return f
            f.Select2(False, 0)                        # − 方向へ並んだので向きを変えてやり直す
            M(self.doc, "EditDelete")
        raise RuntimeError(f"構成部品の直線パターンに失敗: {name}")

    def leaves(self):
        """部品の構成部品をすべて返す（サブアセンブリの中も含む）。"""
        root = M(self.doc.ConfigurationManager.ActiveConfiguration, "GetRootComponent3", True)
        out = []

        def walk(c):
            kids = call0(c, "GetChildren") or ()
            if not kids and c is not root:
                out.append(c)
            for k in kids:
                walk(k)
        walk(root)
        return out

    def show_only(self, keep=None):
        """keep(名前) が真の部品だけを表示する。keep=None で全部を表示に戻す。組立の途中の状態を画像にするときに使う。

        名前は「ユニット-1/部品-3」の形。表示の切り替えだけで、モデルは変更しない（保存しないこと）。
        """
        shown = 0
        for c in self.leaves():
            v = keep is None or bool(keep(c.Name2))
            c.Visible = 1 if v else 0
            shown += v
        M(self.doc, "GraphicsRedraw2")
        return shown

    @timed("拘束状態の確認")
    def unconstrained(self):
        """完全に拘束されていない構成部品の名前。"""
        root = M(self.doc.ConfigurationManager.ActiveConfiguration, "GetRootComponent3", True)
        out = []
        for c in (call0(root, "GetChildren") or ()):
            if c.GetConstrainedStatus != 3 and not c.IsFixed and not call0(c, "IsPatternInstance"):
                out.append(c.Name2)                 # パターンで増やした分は元の 1 個に従うので数えない
        return out

    @timed("干渉チェック")
    def interference(self):
        """干渉している部品対の一覧 [(名前1, 名前2, 体積 mm3)]。"""
        mgr = self.doc.InterferenceDetectionManager
        mgr.TreatCoincidenceAsInterference = False
        mgr.TreatSubAssembliesAsComponents = False
        mgr.IncludeMultibodyPartInterferences = False
        out = []
        for it in (mgr.GetInterferences or ()):
            comps = it.Components or ()
            out.append(tuple(c.Name2 for c in comps) + (it.Volume / MM ** 3,))
        mgr.Done
        return out
