"""設計プロジェクトの共通処理。特定の製品に依らない。

使い方: Project を継承し、部品を作る関数（Part を作って finish で保存）と、アセンブリへ置く関数
（place / ring_of / insert + mate）を書く。寸法は params モジュールの大文字の値を式で参照する。
このクラスが受け持つのは、材料・プロパティの設定と保存、配置した部品の質量集計、
トップアセンブリから見た構成部品の取得、干渉・拘束状態・画像・STEP の出力。
"""
from __future__ import annotations

import functools
import hashlib
import inspect
import json
import math
import re
import shutil
import time
from pathlib import Path

from .core import SW, Asm, M, Part, Profile, call0, timed

I3 = ((1, 0, 0), (0, 1, 0), (0, 0, 1))
FLIP_X = ((-1, 0, 0), (0, 1, 0), (0, 0, -1))        # Y 軸まわり 180°
X_DOWN = ((0, -1, 0), (1, 0, 0), (0, 0, 1))         # 部品の X 軸を下へ（上から入れるボルト）
X_TO_NEG_Z = ((0, 0, -1), (0, 1, 0), (1, 0, 0))     # 部品の X 軸をアセンブリの −Z へ
X_TO_Z = ((0, 0, 1), (0, 1, 0), (-1, 0, 0))         # 部品の X 軸をアセンブリの Z 軸へ
X_TO_Y = ((0, 1, 0), (1, 0, 0), (0, 0, -1))         # 部品の X 軸を上へ、Y 軸を前へ
X_TO_Z_Y_TO_X = ((0, 0, 1), (1, 0, 0), (0, 1, 0))   # 部品の X 軸を Z へ、Y 軸を X へ、Z 軸を上へ

GREY, BLACK, YELLOW, BLUE, ORANGE = (0.75, 0.77, 0.8), (0.12, 0.12, 0.13), (0.95, 0.8, 0.1), (0.2, 0.45, 0.85), (0.9, 0.45, 0.1)


def cached_part(fn):
    """部品を作るメソッドに付ける。作り方と、使った設計変数の値が前回と同じなら、保存済みのファイルを使い回す。

    キャッシュのキーは「メソッドのソース＋swkit のソース＋引数＋その部品が参照した設計変数の値」。
    アセンブリの穴フィーチャが後から部品へ反映されても影響しないよう、保存直後の部品を
    <cad_root>/../.swkit_cache/ に控えておき、使い回すときはそこからコピーする。
    """
    @functools.wraps(fn)
    def wrapper(self, *args):
        key = f"{fn.__name__}{args!r}"
        src = hashlib.sha1((inspect.getsource(fn) + self._kit_src).encode("utf-8")).hexdigest()
        ent = self._cache.get(key)
        # メソッドのソースに出てくる設計変数（式の中の名前と P.NAME の両方）の現在値
        names = sorted(n for n in set(re.findall(r"[A-Z][A-Z0-9_]{2,}", inspect.getsource(fn))) if hasattr(self.P, n))
        deps = {n: repr(getattr(self.P, n)) for n in names}
        if ent and ent["src"] == src and ent["deps"] == deps and all(repr(getattr(self.P, k, None)) == v for k, v in ent["globals"].items()):
            keep = self._cache_dir / ent["file"]
            dst = self.cad_root / ent["rel"]
            if keep.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(keep, dst)
                self.parts[dst.stem] = (dst, ent["mass"], ent["volume"], tuple(ent["cg"]))
                print(f"  部品 {dst.stem:<22} {ent['mass'] * 1000:8.1f} g  （変更なし。保存済みを使用）")
                return dst.stem
        t0 = time.time()
        self._last = None
        name = fn(self, *args)
        path, m, v, cg = self.parts[name]
        keep = f"{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}_{path.name}"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, self._cache_dir / keep)
        self._cache[key] = {"src": src, "deps": deps, "globals": {k: repr(v) for k, v in (self._last or {}).items()}, "file": keep, "rel": str(path.relative_to(self.cad_root)),
                            "mass": m, "volume": v, "cg": list(cg), "seconds": round(time.time() - t0, 1)}
        (self._cache_dir / "index.json").write_text(json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8")
        return name
    return wrapper


class Project:
    def __init__(self, params, cad_root, labels=None, use_cache=True):
        """params: 設計パラメータのモジュール。cad_root: 生成する CAD のルート。labels: 基準平面の式 -> 名前。

        use_cache=False なら、保存済みの部品を使い回さずに全部を作り直す（作った部品は控えに残す）。
        """
        self.P = params
        self.cad_root = Path(cad_root)
        self.labels = dict(labels or {})
        self.sw = SW(params)
        self.sw.close_under(self.cad_root)
        self.parts = {}        # 名前 -> (path, 質量, 体積, 重心)
        self.instances = []    # 配置した部品 1 個ずつの質量・体積・重心（トップアセンブリ座標）
        self.comp = {}         # (ユニット, 部品名) -> [構成部品名]
        self.warned = []
        self._tree = None
        self._last = None
        self._t0 = time.time()
        kit = Path(__file__).parent
        # 部品の形を決めるのは core.py と vendor.py。project.py（手順・出力）を直しても部品は作り直さない
        self._kit_src = "".join((kit / f).read_text(encoding="utf-8") for f in ("core.py", "vendor.py"))
        self._cache_dir = self.cad_root.parent / ".swkit_cache"
        idx = self._cache_dir / "index.json"
        self._cache = json.loads(idx.read_text(encoding="utf-8")) if idx.exists() and use_cache else {}
        self._use_cache = use_cache

    # ------------------------------------------------------------ 文書
    def new_part(self):
        """新しい部品を作り，Part を返す．"""
        return self.sw.new_part()

    def new_asm(self, path):
        """新しいアセンブリを作って path に保存し，Asm を返す．"""
        a = self.sw.new_asm()
        a.labels = dict(self.labels)
        a.save_as(path)
        return a

    @timed("部品の仕上げ（材料・プロパティ）")
    def finish(self, part: Part, path, mat, color=GREY, transparency=0.0, mass=None, **props):
        """材料・色・プロパティを設定して保存する。

        mat は name / rho / sw（SOLIDWORKS 材料名、無ければ空）を持つもの。
        mass を与えると見かけ密度で質量を合わせる（購入品の外形モデル用）。
        """
        path = Path(path)
        if getattr(mat, "sw", ""):
            part.set_material(mat.sw)
        elif mat.rho:
            part.set_density(mat.rho)
        part.solve_equations()                           # 強制再構築はしない（材料・密度の設定には要らない）
        if mass is not None:
            part.set_density(mass / part.mass()[1])
        part.set_color(color, transparency)
        part.set_prop("品名", path.stem)
        part.set_prop("材質", mat.name)
        for k, v in props.items():
            part.set_prop(k, v)
        try:                                             # 非表示で作った文書には表示の操作が効かない
            part.doc.ShowNamedView2("", 7)
            M(part.doc, "ViewZoomtofit2")
        except Exception:
            pass
        part.save_as(path)
        m, v, cg = part.mass()
        self.parts[path.stem] = (path, m, v, cg)
        # この部品が参照した設計変数と値（キャッシュの有効判定に使う）
        self._last = {k: getattr(self.P, k) for k in sorted(part.globals)}
        if part.warnings:
            self.warned.append((path.stem, list(part.warnings)))
        now = time.time()
        print(f"  部品 {path.stem:<22} {m * 1000:8.1f} g  {v * 1e6:8.1f} cm3  警告 {len(part.warnings)}  {now - self._t0:5.0f} 秒")
        self._t0 = now
        return path.stem

    def vendor(self, src, dst, rows, origin, mat, mass, color=ORANGE, axis=False, **props):
        """メーカー配布の 3D データを取り込み、原点と向きをそろえて既製品の部品として保存する。

        rows / origin は配布データの座標系で見た、そろえたあとの軸の向きと原点（swkit.vendor.align）。
        axis=True なら X 軸に基準軸を作る（アセンブリでスケッチの線に一致させる相手）。
        """
        from . import vendor
        doc = timed("メーカーデータの取り込み")(vendor.load)(self.sw, src)
        timed("メーカーデータの向きそろえ")(vendor.align)(doc, rows, origin)
        part = Part(self.sw, doc, self.P)
        if axis:
            part.axis_x()
        return self.finish(part, dst, mat, color, mass=mass, 出典=Path(src).name, **props)

    def existing(self, path, rho=None):
        """既にある部品ファイル（RAPiD Design で取り込んだ MISUMI 品など）を、作り直さずに既製品として登録する。

        形状には手を加えない。rho を与えると密度だけ設定して保存する（配布モデルは材料が未設定のことがある）。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"{path} が無い。swkit.rapid で取り込んでおく")
        part = Part(self.sw, self.sw.open(path), self.P)
        if rho:
            # 密度は 1 回だけ設定する。毎回保存するとファイルの更新時刻が変わり、ユニットの使い回しが効かなくなる
            pm = M(part.ext, "CustomPropertyManager", "")
            if (M(pm, "Get", "swkit_密度") or "") != f"{rho:g}":
                part.set_density(rho)
                part.set_prop("swkit_密度", f"{rho:g}")
                part.save()
        m, v, cg = part.mass()
        self.parts[path.stem] = (path, m, v, cg)
        return path.stem

    # ------------------------------------------------------------ 配置
    def record(self, name, rows, t, unit, comp=None):
        """配置した 1 個の質量・体積・重心を控える。t は数値 mm。"""
        _, m, v, cg = self.parts[name]
        world = tuple(sum(cg[i] * rows[i][k] for i in range(3)) + t[k] for k in range(3))
        self.instances.append({"unit": unit, "part": name, "mass": m, "volume": v, "cg": world})
        if comp is not None:
            self.comp.setdefault((unit, name), []).append(comp.Name2)

    def place(self, asm, name, rows=I3, t=(0, 0, 0), unit=""):
        """軸に平行な姿勢の部品を、アセンブリの基準平面 3 枚に一致させて拘束する。t は式。"""
        comp = asm.place(self.parts[name][0], rows, t)
        self.record(name, rows, [asm.val(c) for c in t], unit, comp)
        return comp

    def ring_of(self, asm, name, x, pcd, n, unit, label):
        """X 軸まわりの PCD 上の 1 個を合致で拘束し、構成部品の円形パターンで n 個にする。"""
        seed = self.place(asm, name, t=(x, f"{pcd} / 2", 0), unit=unit)
        asm.circular(seed, n, label)
        r, x0 = asm.val(pcd) / 2, asm.val(x)
        for i in range(1, n):                            # 2 個目以降は X 軸まわりに回した姿勢で控える（重心の向きも回る）
            a = math.radians(360 * i / n)
            c, s = math.cos(a), math.sin(a)
            self.record(name, ((1, 0, 0), (0, c, s), (0, -s, c)), (x0, r * c, r * s), unit)

    def row_of(self, asm, name, t, pitch, n, unit, label, rows=I3):
        """位置 t の 1 個を合致で拘束し、構成部品の直線パターンで X の + 方向へ n 個に並べる。"""
        seed = self.place(asm, name, rows, t, unit=unit)
        if n > 1:
            asm.linear(seed, n, pitch, label)
        t0, dx = [asm.val(c) for c in t], asm.val(pitch)
        for i in range(1, n):
            self.record(name, rows, (t0[0] + i * dx, t0[1], t0[2]), unit)
        return seed

    def comps(self, top, unit, name, first_only=False):
        """トップアセンブリから見た構成部品（サブアセンブリ unit の中の部品 name）。

        first_only: 同じ部品ファイルを複数個使っているとき 1 個目だけ返す。アセンブリの穴フィーチャを
        部品へ反映するとき、同じファイルへ同じ穴が重複して入るのを避ける。
        """
        if self._tree is None:
            self._tree = {}
            root = M(top.doc.ConfigurationManager.ActiveConfiguration, "GetRootComponent3", True)

            def walk(c):
                for ch in (call0(c, "GetChildren") or ()):
                    self._tree[ch.Name2] = ch
                    walk(ch)
            walk(root)
        found = [self._tree.get(f"{unit}-1/{n}") for n in self.comp[(unit, name)]]
        if None in found:
            raise RuntimeError(f"構成部品が見つからない: {unit}/{name}")
        return found[:1] if first_only else found

    # ------------------------------------------------------------ アセンブリ先行の手順
    @staticmethod
    def shared_hole(base, w, pts, size, name, targets, depth, from_pos=True):
        """相手部品と共締めする穴の宣言。build() がトップアセンブリの穴フィーチャとして開け、加工部品へ反映する。

        base / w / pts / size / from_pos は Doc.hole と同じ。depth は必ず指定する（貫通も「板厚＋α」で指示する。
        作成後に貫通へ変更すると、部品への反映が作り直されて重複が残る）。
        targets: 穴を開ける部品 [(ユニット名, 部品名)] または [(ユニット名, 部品名, True)]。
                 3 つ目が True なら、同じ部品ファイルを複数個使っていても 1 個目だけを対象にする
                 （全部を対象にすると、同じ穴が同じ部品ファイルへ二重に反映される）。
                 購入品は対象に入れない。購入品は自身の図面どおりの穴を部品側に持たせる。
        """
        return {"base": base, "w": w, "pts": pts, "size": size, "name": name, "targets": targets, "depth": depth, "from_pos": from_pos}

    def build(self, top_path, units, holes=(), layout=None, title=None, out_dir=None, image_dir=None, prefix="asm"):
        """アセンブリから設計する手順をまとめて実行する。

        units : [(ユニット名, 保存フォルダ, そのサブアセンブリを組み立てる関数 fn(asm))]
        holes : shared_hole() の一覧
        layout: トップアセンブリにレイアウトスケッチを描く関数 fn(top)

        速く終わらせるための段取りはここで決める（設計ごとのコードには書かない）。
          - 部品は cached_part で、変更のないものを使い回す
          - 穴フィーチャは 1 つごとに、トップアセンブリに載っている全構成部品の再構築を起こす。
            そこで、穴の対象になるユニットだけを先に載せて穴を開け、残りのユニットはそのあとで載せる
          - 穴を開けている間は、画面とフィーチャツリーの更新を止める
        """
        top_path = Path(top_path)
        t0 = time.time()

        def lap(msg):
            print(f"{msg}  [{time.time() - t0:5.0f} 秒]")
        lap("1. アセンブリを先に作る")
        top = self.new_asm(top_path)
        subs = {name: self.new_asm(Path(d) / f"{name}.SLDASM") for name, d, _ in units}
        if layout is not None:
            lap("2. レイアウトスケッチ")
            layout(top)
            top.save()
        lap("3. 部品とサブアセンブリ")
        hole_units = {t[0] for h in holes for t in h["targets"]}
        for name, _, fn in units:
            lap(f" [{name}]")
            key = None if name in hole_units else self._unit_key(fn)
            if key is not None and self._unit_restore(name, key, subs):
                continue
            n0 = len(self.instances)
            fn(subs[name])
            subs[name].rebuild()
            subs[name].set_prop("品名", name)
            subs[name].save()
            if key is not None:
                self._unit_keep(name, key, subs[name], n0)
        first = [name for name in subs if any(name == t[0] for h in holes for t in h["targets"])]
        lap(f"4. トップアセンブリ（穴に関わるユニット: {'、'.join(first) or 'なし'}）")
        for name in first:
            top.place(subs[name].path)
        top.rebuild()
        if holes:
            lap(f"5. 共締め穴 {len(holes)} フィーチャ（アセンブリの穴 → 加工部品へ反映）")
            SW.quiet(top.doc)
            for h in holes:
                scope = [c for t in h["targets"] for c in self.comps(top, t[0], t[1], first_only=len(t) > 2 and t[2])]
                top.hole(h["base"], h["w"], h["pts"], h["size"], h["name"], from_pos=h["from_pos"], depth=h["depth"], scope=scope)
            SW.quiet(top.doc, False)
        lap("6. トップアセンブリ（残りのユニット）")
        for name in subs:
            if name not in first:
                top.place(subs[name].path)
        top.rebuild()
        top.set_prop("品名", title or top_path.stem)
        top.save()
        lap("7. 出力")
        self.outputs(top, subs, out_dir or self.cad_root, image_dir or self.cad_root, top_path.with_suffix(".STEP"), prefix)
        lap("完了")
        print("\n■ 処理別の所要時間（重複なし）")
        prof = Profile.report(time.time() - t0)
        Path(out_dir or self.cad_root).joinpath("build_profile.json").write_text(
            json.dumps({"total_seconds": round(time.time() - t0, 1), "by_kind": prof}, ensure_ascii=False, indent=1), encoding="utf-8")
        return top, subs

    # ------------------------------------------------------------ ユニットの使い回し
    def _unit_key(self, fn):
        """既製品を並べるだけのユニットなら、その内容を表すキーを返す。部品を作るユニットは None（使い回さない）。

        キーは「ユニットを組み立てるメソッドと、そこから呼ぶ設計側のメソッドのソース＋そこに出てくる設計変数の値」。
        部品を作るユニットは、部品ごとの cached_part に任せる（穴フィーチャが後から部品へ反映されるため）。
        """
        if not self._use_cache:
            return None
        seen, src = set(), []

        def add(f):
            code = inspect.getsource(f)
            src.append(code)
            for n in re.findall(r"self\.(\w+)\(", code):
                m = getattr(type(self), n, None)
                if m is not None and n not in seen and not hasattr(Project, n):
                    seen.add(n)
                    add(m)
        add(fn)
        text = "".join(src)
        if re.search(r"new_part\(|\.finish\(|\.vendor\(|\.cable\(", text):
            return None
        names = sorted(n for n in set(re.findall(r"[A-Z][A-Z0-9_]{2,}", text)) if hasattr(self.P, n))
        deps = "".join(f"{n}={getattr(self.P, n)!r};" for n in names)
        project_src = Path(__file__).read_text(encoding="utf-8")
        return hashlib.sha1((text + deps + self._kit_src + project_src).encode("utf-8")).hexdigest()

    @staticmethod
    def _stamp(path):
        st = Path(path).stat()
        return [st.st_size, int(st.st_mtime)]

    def _unit_keep(self, name, key, asm, n0):
        """組み上がったユニットを控える。"""
        keep = f"unit_{hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]}.SLDASM"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(asm.path, self._cache_dir / keep)
        inst = self.instances[n0:]
        parts = sorted({i["part"] for i in inst})
        self._cache[f"unit:{name}"] = {
            "key": key, "file": keep, "instances": inst,
            "comp": {k[1]: v for k, v in self.comp.items() if k[0] == name},
            "parts": {n: {"path": str(self.parts[n][0]), "stamp": self._stamp(self.parts[n][0]), "mass": self.parts[n][1],
                          "volume": self.parts[n][2], "cg": list(self.parts[n][3])} for n in parts}}
        (self._cache_dir / "index.json").write_text(json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8")

    def _unit_restore(self, name, key, subs):
        """控えが今の内容と同じなら、保存済みのサブアセンブリに差し替える。差し替えたら True。"""
        ent = self._cache.get(f"unit:{name}")
        if not ent or ent["key"] != key or not (self._cache_dir / ent["file"]).exists():
            return False
        for n, q in ent["parts"].items():            # 使っている部品ファイルが控えたときのままか
            if not Path(q["path"]).exists() or self._stamp(q["path"]) != q["stamp"]:
                return False
        asm = subs[name]
        path = asm.path
        self.sw.app.CloseDoc(call0(asm.doc, "GetTitle"))
        shutil.copy2(self._cache_dir / ent["file"], path)
        new = Asm(self.sw, self.sw.open(path), self.P)
        new.path = path
        new.labels = dict(self.labels)
        subs[name] = new
        for n, q in ent["parts"].items():
            self.parts[n] = (Path(q["path"]), q["mass"], q["volume"], tuple(q["cg"]))
        self.instances.extend(ent["instances"])
        for part, comps in ent["comp"].items():
            self.comp[(name, part)] = list(comps)
        print(f"  ユニット {name}: 変更なし。保存済みのサブアセンブリを使用（構成部品 {len(ent['instances'])} 個）")
        return True

    # ------------------------------------------------------------ 出力
    def outputs(self, top, subs, out_dir, image_dir, step_path, prefix):
        """拘束状態・干渉・質量・画像・STEP を出力する。subs: {名前: Asm}。"""
        out_dir, image_dir = Path(out_dir), Path(image_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        image_dir.mkdir(parents=True, exist_ok=True)
        free = {}
        for name, a in list(subs.items()) + [("トップアセンブリ", top)]:
            if a.warnings:
                self.warned.append((name, list(a.warnings)))
            free[name] = a.unconstrained()
            print(f"   {name}: 拘束されていない構成部品 {free[name] or 'なし'}")
        (out_dir / "cad_mass.json").write_text(json.dumps(self.instances, ensure_ascii=False, indent=1), encoding="utf-8")
        inter = top.interference()
        (out_dir / "cad_interference.json").write_text(json.dumps(inter, ensure_ascii=False, indent=1), encoding="utf-8")
        (out_dir / "cad_warnings.json").write_text(
            json.dumps({"warnings": self.warned, "unconstrained": free}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"干渉 {len(inter)} 件")
        for it in inter:
            print("   ", it)
        self.sw.activate(top.doc)
        SW.quiet(top.doc, False)
        try:                                             # 画像には形だけを出す（スケッチ・平面・軸・原点を隠す）
            self.sw.app.FrameState = 1                   # ウィンドウを最大化
            top.ext.SetUserPreferenceToggle(198, 0, True)
        except Exception:
            pass
        for view, tag in ((7, "iso"), (1, "front"), (5, "top"), (4, "right")):
            top.doc.ShowNamedView2("", view)
            M(top.doc, "ViewZoomtofit2")
            top.export(image_dir / f"{prefix}_{tag}.png")
        # 断面: 正面の基準平面で切り、正面と等角から書き出す（被覆や筒の中の構成を図にする）
        if top.section("front"):
            for view, tag in ((1, "section"), (7, "section_iso")):
                top.doc.ShowNamedView2("", view)
                M(top.doc, "ViewZoomtofit2")
                top.export(image_dir / f"{prefix}_{tag}.png")
            top.section(on=False)
        try:
            top.ext.SetUserPreferenceToggle(198, 0, False)
        except Exception:
            pass
        top.doc.ShowNamedView2("", 7)
        M(top.doc, "ViewZoomtofit2")
        top.export(step_path)
        top.save()
        m, v, cg = top.mass()
        print(f"全体 質量 {m:.2f} kg, 実体体積 {v * 1e3:.2f} L, 重心 {tuple(round(c, 1) for c in cg)}")
        print("警告のあった文書:", [w[0] for w in self.warned] or "なし")
