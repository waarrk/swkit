# API 一覧

ソースから自動で生成した一覧．説明は各関数の説明文の 1行目である．名前が `_` で始まるものは内部用のため載せていない．

## swkit.core

SOLIDWORKS API(COM) の薄いラッパ。

| 関数 | 説明 |
| --- | --- |
| `M(obj, name, *args)` | メソッドを確実にメソッドとして呼ぶ（プロパティ取得と誤認されるのを防ぐ）。 |
| `call0(obj, name)` | 引数なしメソッド/プロパティのどちらでも値を得る（遅延バインディング対策）。 |
| `process_memory_mb(pid)` | プロセスが確保しているメモリ（プライベートバイト）を MB で返す。取れなければ 0。 |

### Profile

処理の種類ごとの所要時間。入れ子になった処理は、内側の時間を外側から引いて重複なく数える。

| メソッド | 説明 |
| --- | --- |
| `timed(label)` | 処理の所要時間を種類ごとに積算するデコレータ． |
| `span(label)` | with で囲んだ区間の所要時間を、種類 label に積算する（関数の一部だけを測るとき）。 |
| `report(total=None)` | 処理の種類ごとの回数と所要時間を表示し，辞書で返す． |

### DialogWatch

生成中に SOLIDWORKS が出したダイアログを記録する（別スレッドで窓を見張る）。

| メソッド | 説明 |
| --- | --- |
| `__init__(pid, period=0.5)` |  |
| `run()` |  |
| `stop()` | 見張りを止め、出たダイアログの一覧（題名、本文、出ていた秒数、自動で押したか）を返す。 |

### SW



| メソッド | 説明 |
| --- | --- |
| `__init__(params=None)` | SOLIDWORKS に接続し，生成中だけ画面更新と視点のアニメーションを止める．params は設計値のモジュール． |
| `restore()` | 変更した SOLIDWORKS の設定を元に戻す．終了時に必ず呼ぶ． |
| `quiet(doc, on=True)` | 文書の画面更新とフィーチャツリー更新を止める（on=False で戻す）。表示モードでは止めない。 |
| `new_part()` | 新しい部品を作り，Part を返す． |
| `new_asm()` | 新しいアセンブリを作り，Asm を返す． |
| `open(path, kind=None)` | 部品またはアセンブリを開き，文書オブジェクトを返す． |
| `activate(doc)` | 文書を前面にする．寸法へ関係式を付ける前に必要となる． |
| `close_under(root)` | root 配下に保存された文書だけを閉じる。ほかに開いている文書には触れない。 |
| `transform(rows, t_mm)` | rows: 部品の x,y,z 軸がアセンブリ座標で向く単位ベクトル 3本。t_mm: 原点位置。 |

### Sketch

拘束と駆動寸法で完全定義するスケッチ。

| メソッド | 説明 |
| --- | --- |
| `__init__(owner, base, feature, revolve=False, edit=False)` | 基準平面にスケッチを開く．Part.sketch と Asm.sketch から呼ばれる． |
| `val(e)` | 式を設計値で評価して数値 mm を返す． |
| `centerline(length)` | 回転軸。原点から +u 方向。 |
| `poly(pts, close=True, free=())` | 折れ線。水平・鉛直の辺には拘束を付け、位置を式ごとの寸法で決める。 |
| `rect(u0, u1, v0, v1)` | 辺が軸に平行な長方形．4辺の位置を式で与える． |
| `circle(u, v, d)` | 中心 (u, v)，直径 d の円． |
| `half_circle(r, upper)` | 原点中心の半円弧（筒受けの受け面）。端点は同じ位置にある線の端点と結合される。 |
| `arc(p0, p1, r, toward, concentric=None, center=None)` | p0 から p1 への円弧（劣弧）。半径 r は式につなぐ。 |
| `point(u, v)` | 位置決め用の点（レイアウトで構成部品を合致させる相手）。 |
| `hole_points(pts)` | 穴位置スケッチの点。穴ウィザードが置いた点はそのまま使い、残りを追加する。 |
| `convert(pts, construction=False)` | エンティティ変換: モデル座標 pts (x, y, z) mm を通る既存のエッジを、このスケッチへ投影する。 |
| `offset(segs, d, toward)` | オフセット: 線 segs（convert の戻り値など）を距離 d だけ平行に移した線を作る。距離の寸法は式につなぐ。 |
| `define()` | 拘束と寸法を付けて完全定義にする．フィーチャを作る操作から自動で呼ばれる． |

### Doc



| メソッド | 説明 |
| --- | --- |
| `__init__(sw, doc, params=None)` | 文書を包み，関係式の管理を始める． |
| `warn(msg)` | 警告を記録して表示する． |
| `solve_equations()` | ためておいた関係式をまとめて評価する。 |
| `save_as(path)` | 関係式を評価してから名前を付けて保存する． |
| `save()` | 関係式を評価してから上書き保存する． |
| `rebuild()` | 関係式を評価して再構築する． |
| `set_prop(name, value)` | ユーザー定義プロパティを設定する． |
| `export(path)` | STEP などの形式で書き出す．形式は拡張子で決まる． |
| `section(base='front', on=True, reverse=False)` | 基準平面 base（front / top / right）で切った断面表示にする。on=False で元に戻す。 |
| `image(path, view=7)` | 形だけの画像を書き出す（スケッチ・平面・軸・原点は隠す）。view: 7 = 等角、1 = 正面、5 = 上面、4 = 右側面。 |
| `planes()` | 正面・平面・右側面（言語に依らず先頭 3 つの参照平面）。1 回探したら覚えておく。 |
| `plane_names()` | 正面・平面・右側面の名前を返す． |
| `ref_plane(j, expr)` | 軸 j（0 = X, 1 = Y, 2 = Z）に垂直で、座標が expr の基準平面。0 なら既定の平面を返す。 |
| `planes_of(doc)` | 文書の正面・平面・右側面の名前。 |
| `title()` | 文書の題名を返す． |
| `mass()` | (質量 kg, 体積 m3, 重心 mm)。 |
| `val(e)` | 式を設計値で評価して数値 mm を返す． |
| `link(dim, expr, scale=1.0)` | 寸法 dim を式 expr につなぐ。scale=2 は半径の式を直径寸法へつなぐ場合。 |
| `link_feature(feat, expr)` | フィーチャの寸法（板厚・フィレット半径など）を式につなぐ。 |
| `hole(base, w, pts, size, name, from_pos=True, depth=None, thread=None, scope=None)` | 穴ウィザードで JIS のキリ穴（size="Ø5.5"）またはタップ穴（size="M4"）をあける。 |

### Part

加工工程ごとにフィーチャを分けて作る部品。寸法は設計パラメータの関係式で駆動する。

| メソッド | 説明 |
| --- | --- |
| `__init__(sw, doc, params=None)` | 部品の文書を包む． |
| `set_material(name, db='SOLIDWORKS Materials')` | SOLIDWORKS の材料を割り当てる． |
| `set_density(rho)` | 密度 kg/m3 を設定する． |
| `set_color(rgb, transparency=0.0)` | 色と透明度を設定する． |
| `sketch(base, revolve=False)` | base: front / top / right。スケッチ平面は部品の加工基準に合わせた基準平面を使う。 |
| `extrude_mid(t, name)` | スケッチ面を中心に両側へ合計 t（板厚、またはほぞの厚さ）。 |
| `revolve(name, cut=False)` | スケッチ内の中心線まわりに 360°（旋削）。 |
| `cut_through(name)` | スケッチ面の両側へ全貫通（窓・角穴の切り抜き）。 |
| `base_flange(width, t, r, name)` | 板金のベースフランジ。開いた輪郭（曲げ断面）をスケッチ面の両側へ合計 width 押し出す。 |
| `sketch_face(base, w, at=(0, 0), from_pos=True)` | 部品の平らな面にスケッチを開く。base: 面の向き（front / top / right）、w: 法線方向の位置、at: 面内の 1 点。 |
| `sketched_bend(angle, r, fixed, name, flip=False)` | 板金の平らな面に描いた 1 本の線で板を折る（スケッチ曲げ）。平板を抜いてから曲げる工程をそのまま表す。 |
| `cable(pts, d, name)` | 点列 pts（モデル座標 mm）を通る 3D スプラインに沿った丸断面のケーブル。ほかのボディとは結合しない。 |
| `keep_sketch(name)` | フィーチャにせずスケッチのまま残す（レイアウト用）。 |
| `find_edges(pts)` | モデル座標 (x, y, z) mm の点を通るエッジを返す。表示の向きに左右されないよう形状から探す。 |
| `fillet(pts, r, name)` | pts を通るエッジに半径 r のフィレット。半径は式につなぐ。 |
| `chamfer(pts, c, name)` | pts を通るエッジに C 面取り（45°）。 |
| `axis_x()` | X 軸（正面∩平面）の基準軸。旋削部品の中心軸で、パターンの基準に使う。 |
| `mirror(feats, plane, name)` | フィーチャを基準平面 plane（front / top / right）で対称に写す。 |
| `circular(feats, n, name)` | フィーチャを中心軸まわりに等配 n 個。 |
| `linear(feats, pitch, expect_x, name)` | カットフィーチャを中心軸（X）方向へピッチ pitch で 2 個に並べる。 |

### Asm

構成部品はレイアウトスケッチと基準平面に合致で拘束する。固定は使わない。

| メソッド | 説明 |
| --- | --- |
| `__init__(sw, doc, params=None)` | アセンブリの文書を包む． |
| `stem()` | 拡張子を除いた文書名を返す． |
| `insert(path, rows=((1, 0, 0), (0, 1, 0), (0, 0, 1)), t=(0, 0, 0))` | 構成部品を姿勢 rows・位置 t(mm) の近くに置く。拘束は合致で別に付ける。 |
| `mate(kind, e1, e2)` | 2 つの要素を合致させる。向きは現在の姿勢に近い側を採る。 |
| `mate_distance(comp, what, j, expr)` | 構成部品の基準平面 what を、アセンブリの既定の平面（軸 j に垂直）から式 expr の距離に合致させる。 |
| `place(path, rows=((1, 0, 0), (0, 1, 0), (0, 0, 1)), t=(0, 0, 0), distance=False)` | 軸に平行な姿勢の構成部品を、基準平面 3 枚との一致で拘束する。 |
| `sketch(base, plane=None)` | アセンブリのレイアウトスケッチ。plane を省くと既定の平面に描く。 |
| `keep_sketch(name)` | スケッチを閉じ，名前を付けてレイアウトスケッチとして残す． |
| `axis_x()` | X 軸（正面∩平面）の基準軸。構成部品の円形パターンの中心。 |
| `circular(comp, n, name)` | 構成部品を中心軸まわりに等配 n 個（構成部品の円形パターン）。 |
| `axis_of(axis='x')` | 基準平面 2 枚の交線の基準軸（x / y / z）。構成部品パターンの方向に使う。X 軸は axis_x と同じ軸。 |
| `linear(comp, n, pitch, name, axis='x')` | 構成部品を軸 axis（x / y / z）の + 方向へピッチ pitch で n 個に並べる（構成部品の直線パターン）。 |
| `driven(comp, host, feature, name)` | 構成部品を、相手の部品 host が持つパターン feature に従わせて並べる（パターン駆動構成部品パターン）。 |
| `leaves()` | 部品の構成部品をすべて返す（サブアセンブリの中も含む）。 |
| `show_only(keep=None)` | keep(名前) が真の部品だけを表示する。keep=None で全部を表示に戻す。組立の途中の状態を画像にするときに使う。 |
| `unconstrained()` | 完全に拘束されていない構成部品の名前。 |
| `interference()` | 干渉している部品対の一覧 [(名前1, 名前2, 体積 mm3)]。 |

## swkit.project

設計プロジェクトの共通処理。特定の製品に依らない。

| 関数 | 説明 |
| --- | --- |
| `cached_part(fn)` | 部品を作るメソッドに付ける。作り方と、使った設計変数の値が前回と同じなら、保存済みのファイルを使い回す。 |

### Project



| メソッド | 説明 |
| --- | --- |
| `__init__(params, cad_root, labels=None, use_cache=True)` | params: 設計パラメータのモジュール。cad_root: 生成する CAD のルート。labels: 基準平面の式 -> 名前。 |
| `new_part()` | 新しい部品を作り，Part を返す． |
| `new_asm(path)` | 新しいアセンブリを作って path に保存し，Asm を返す． |
| `finish(part, path, mat, color=GREY, transparency=0.0, mass=None, **props)` | 材料・色・プロパティを設定して保存する。 |
| `vendor(src, dst, rows, origin, mat, mass, color=ORANGE, axis=False, **props)` | メーカー配布の 3D データを取り込み、原点と向きをそろえて既製品の部品として保存する。 |
| `existing(path, rho=None)` | 既にある部品ファイル（RAPiD Design で取り込んだ MISUMI 品など）を、作り直さずに既製品として登録する。 |
| `kit(path, fn, title=None)` | 組になった部品をサブアセンブリにまとめ、部品と同じ名前で置けるようにする。名前（ファイル名）を返す。 |
| `record(name, rows, t, unit, comp=None)` | 配置した 1 個の質量・体積・重心を控える。t は数値 mm。組（kit）は中の部品に展開して控える。 |
| `place(asm, name, rows=I3, t=(0, 0, 0), unit='', distance=False)` | 軸に平行な姿勢の部品を、アセンブリの基準平面 3 枚に一致させて拘束する。t は式。 |
| `ring_of(asm, name, x, pcd, n, unit, label)` | X 軸まわりの PCD 上の 1 個を合致で拘束し、構成部品の円形パターンで n 個にする。 |
| `row_of(asm, name, t, pitch, n, unit, label, rows=I3)` | 位置 t の 1 個を合致で拘束し、構成部品の直線パターンで X の + 方向へ n 個に並べる。 |
| `group_of(asm, items, pitch, n, label, axis='x', distance=False)` | 組になった部品をまとめて，構成部品の直線パターンで軸 axis の + 方向へ n 組に並べる。 |
| `follow_of(asm, items, host, feature, label, distance=False)` | 組になった部品を、相手の部品 host のパターン feature に従わせて並べる（パターン駆動構成部品パターン）。 |
| `comps(top, unit, name, first_only=False)` | トップアセンブリから見た構成部品（サブアセンブリ unit の中の部品 name）。 |
| `shared_hole(base, w, pts, size, name, targets, depth, from_pos=True)` | 相手部品と共締めする穴の宣言。build() がトップアセンブリの穴フィーチャとして開け、加工部品へ反映する。 |
| `build(top_path, units, holes=(), layout=None, title=None, out_dir=None, image_dir=None, prefix='asm')` | アセンブリから設計する手順をまとめて実行する。 |
| `outputs(top, subs, out_dir, image_dir, step_path, prefix)` | 拘束状態・干渉・質量・画像・STEP を出力する。subs: {名前: Asm}。 |

## swkit.vendor

メーカー配布の 3D データ（STEP / IGES / SLDPRT）を調べて、既製品の部品として取り込む。

| 関数 | 説明 |
| --- | --- |
| `load(sw, path)` | STEP / IGES / SLDPRT を開いて文書を返す。 |
| `bodies(doc)` | ソリッドとサーフェスの全ボディ。 |
| `bbox(doc)` | 全ボディを囲む箱 (min xyz, max xyz) mm。 |
| `cylinders(doc, min_r=1.0)` | 円筒面の (半径 mm, 軸上の点 mm, 軸方向) を面積の大きい順に返す。 |
| `planes(doc, axis, tol=0.001)` | 法線が axis（0/1/2）に平行な平面の、軸方向の位置 mm → 面積 mm2。 |
| `align(doc, rows, origin)` | ボディを動かして、部品の原点と向きを決まった形にそろえる。 |
| `main()` |  |

## swkit.rapid

MISUMI RAPiD Design（SOLIDWORKS アドイン）から、型番を指定して部品モデルを取り込む。

| 関数 | 説明 |
| --- | --- |
| `fetch_one(spec, dest)` | 型番 spec のモデルを取り込み、dest/MISUMI_<型番>.SLDPRT に保存する。プロパティの辞書を返す。 |
| `fetch(specs, dest)` | 型番のリストを取り込む。保存先に既にある型番は飛ばす。結果は dest/MISUMI_parts.csv に追記・更新する。 |

## swkit.docgen

Markdown の原稿から、顧客へ提出できる体裁の文書（Word と PDF）を作る。

| 関数 | 説明 |
| --- | --- |
| `parse(path)` | 原稿を文書情報と本文に分ける． |
| `build(src)` | 原稿から Word 文書を作り，保存先のパスを返す．文書管理台帳があれば照合と更新を行う． |
| `to_pdf(docx)` | Word で開いて目次とページ番号を計算し、docx を保存し直して PDF を書き出す。 |
| `main()` | コマンドラインから Word と PDF を生成する． |

### Register

文書管理台帳。文書番号は台帳に登録してから使う。発行（docx・PDF の生成）のたびに版・発行日・提出物を書き戻す。

| メソッド | 説明 |
| --- | --- |
| `__init__(path)` | 文書管理台帳を読む． |
| `find(src)` | 原稿の上位フォルダから文書管理台帳を探す．無ければ None． |
| `row(number)` | 文書番号の行を返す． |
| `check(meta, src)` | 原稿の文書番号・名称・版を台帳と照合し，合わなければ中止する． |
| `issued(meta, src, out)` | 発行した版・発行日・状態・提出物の場所を台帳に書き戻す． |

## swkit.audit

モデルの中の「使われていない基準平面」と「無駄なスケッチ」を数える（点検用）。何も変更しない。

| 関数 | 説明 |
| --- | --- |
| `audit(sw, path)` | 1つの文書の基準平面とスケッチを数え，使われていないものを返す． |
| `main()` | コマンドラインから点検を実行する． |

## swkit.cli

swkit のコマンド。

| 関数 | 説明 |
| --- | --- |
| `installed_solidworks()` | 入っている SOLIDWORKS の一覧 [(COM 名, 年, 実行ファイル)]。新しい順。 |
| `find_templates(exe)` | SOLIDWORKS の既定の場所から部品・アセンブリのテンプレートを探す。見つからなければ空文字。 |
| `load_config()` | PC ごとの設定ファイルを読む． |
| `doctor()` | 環境を点検して結果を表示する．問題が無ければ 0 を返す． |
| `install_skill()` | 設計者スキルを利用者のスキルフォルダへ入れる． |
| `setup()` | 設定ファイルを作り，設計者スキルを入れ，環境を点検する． |
| `new(name, where='.', code=None, into=False)` | プロジェクトの骨組みを作る。code は文書番号の先頭に付ける英数字（例: UWD01）。 |
| `main(argv=None)` | コマンドを解釈して実行する． |

## swkit.config

PC ごとに違う設定。設定ファイルと環境変数で上書きできる。
