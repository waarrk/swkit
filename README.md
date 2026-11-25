# swkit

SOLIDWORKS を Python（COM）から操作して、**人が後から開いて直せるモデル**を作るためのライブラリです。
特定の製品に依りません。設計ごとのコードは、設計パラメータのモジュールと `Project` の派生クラスだけを書きます。

## 何をしてくれるか

| モジュール | 内容 |
| --- | --- |
| `swkit.core` | 完全定義スケッチ（拘束・中心線の対称・等しい値・原点基準の寸法）、関係式、フィーチャ（押し出し・回転・カット・フィレット・面取り・ミラー・円形/直線パターン・板金・ケーブルのスイープ）、JIS の穴ウィザード、エンティティ変換とオフセット、アセンブリの合致、処理別の時間計測 |
| `swkit.project` | 材料・プロパティの設定、基準平面への合致での配置、質量集計、アセンブリから加工部品へ反映する共締め穴、干渉・拘束状態の確認、画像・STEP の書き出し、部品のキャッシュ |
| `swkit.vendor` | メーカー配布の 3D データ（STEP / IGES / SLDPRT）の取り込みと向きそろえ |
| `swkit.inspect_part` | 部品のフィーチャ構成・スケッチの定義状態・関係式の一覧（点検用） |
| `swkit.rapid` | MISUMI RAPiD Design から型番指定で部品モデルを取り込む（画面操作） |
| `swkit.cli` | `swkit doctor / setup / new / skill`（導入と点検、プロジェクトの骨組み） |
| `swkit.config` | PC ごとの設定（SOLIDWORKS の版、テンプレート） |

モデルの作り方の決まりは [src/swkit/templates/skill/MODELING_RULES.md](src/swkit/templates/skill/MODELING_RULES.md) にあります。

## 必要なもの

- Windows、SOLIDWORKS（2024 で確認）
- Python 3.10 以上、pywin32

## 導入（どの PC でも同じ手順）

```
git clone git@github.com:waarrk/swkit.git
python -m venv .venv
.venv\Scripts\pip install -e swkit[rapid]
.venv\Scripts\swkit setup
```

`swkit setup` が行うこと:

1. 入っている SOLIDWORKS の版とテンプレートを調べて、PC ごとの設定 `%USERPROFILE%\.swkit.json` を作る
2. 設計者スキル `swkit-designer` を `%USERPROFILE%\.claude\skills\` に入れる
3. `swkit doctor` で環境を点検する（SOLIDWORKS、テンプレート、pywin32、RAPiD Design、スキル）

設計者スキルは、Claude Code が SOLIDWORKS の設計作業をするときに読む手順書です。完全定義スケッチ、加工工程ごとのフィーチャ、
フィレット・穴ウィザード・パターンの使い方、アセンブリからの設計、購入品の実データと在庫確認、部品表、設計レビューなど、
実際の設計で設計者から受けた指摘をまとめてあります。これを入れた PC では、同じ決まりで設計が始まります。

## 設計を始める

```
.venv\Scripts\swkit new <プロジェクト名>
cd <プロジェクト名>
python src\<名前>uild.py
```

`swkit new` は、設計パラメータ（`ANALYSIS/params.py`）、生成スクリプトの見本（台板とブロックを共締めする最小の例）、
プロジェクトの決まり（`CLAUDE.md`）、フォルダ構成を作ります。見本を自分の設計に書き換えて使います。

## 設定

設定は `swkit setup` が作ります。手で直すときは `%USERPROFILE%\.swkit.json` を編集します（環境変数 `SWKIT_PROGID` などでも上書きできます）。
テンプレートを空にすると、SOLIDWORKS の既定テンプレートを使います。

```json
{
  "progid": "SldWorks.Application.32",
  "part_template": "D:/templates/部品.prtdot",
  "asm_template": "D:/templates/アセンブリ.asmdot"
}
```

## 使い方

設計値を名前付きの変数に置き、スケッチの座標と寸法を変数名の式で書きます。
寸法は SOLIDWORKS のグローバル変数と関係式につながるので、生成後に変数を変えれば形が追従します。

```python
import types
from swkit import SW

P = types.SimpleNamespace(W=120.0, H=80.0, T=10.0, RIM=15.0)   # 設計パラメータ
sw = SW(P)
try:
    p = sw.new_part()
    s = p.sketch("front")
    s.rect("-W / 2", "W / 2", "-H / 2", "H / 2")               # 中心線に対称、全幅の寸法 1 本ずつ
    p.extrude_mid("T", "板")
    s = p.sketch("front")
    edges = s.convert([(0, "H / 2", "T / 2"), ("W / 2", 0, "T / 2"),
                       (0, "-H / 2", "T / 2"), ("-W / 2", 0, "T / 2")], construction=True)   # エンティティ変換
    s.offset(edges, "RIM", toward=(0, 0))                      # 外形から一定幅の内側輪郭
    p.cut_through("窓")
    p.save_as("plate.SLDPRT")
finally:
    sw.restore()
```

アセンブリから設計する流れ（レイアウトスケッチ → 部品とサブアセンブリ → 基準平面への合致 →
アセンブリの穴フィーチャを加工部品へ反映 → 干渉・拘束の確認と書き出し）は `swkit.Project.build` にあります。
`Project` を継承して、部品を作るメソッドとユニットを組むメソッドを書きます。

```
python examples/plate_with_offset_window.py   # 動作確認用の最小の例
python -m swkit.inspect_part <部品.SLDPRT>     # 出来上がった部品の点検
```

## 注意

- SOLIDWORKS を操作するスクリプトを 2 本同時に走らせないでください。
- 実行中は表示更新と視点アニメーションを止めます。`SW.SHOW = True` にすると、作る過程を画面で見られます（遅くなります）。
- 終了時に `sw.restore()` を呼ぶと、変えた SOLIDWORKS の設定を元に戻します。
