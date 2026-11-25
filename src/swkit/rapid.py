"""MISUMI RAPiD Design（SOLIDWORKS アドイン）から、型番を指定して部品モデルを取り込む。

    python -m swkit.rapid <保存先フォルダ> <型番> [<型番> ...]

RAPiD Design には公開された API が無いので、画面を操作して取り込む。
  1. RAPiD Design の窓の検索欄へ型番を貼り付け、候補（型番ショートカット）を選ぶ
  2. 「SOLIDWORKS で、CAD モデルを開く」の「インポート」を UI オートメーションで押す
  3. SOLIDWORKS に開いたモデルの型番（プロパティ SpecNumber）を確かめ、保存先へ MISUMI_<型番>.SLDPRT として写しを保存して閉じる
取り込んだモデルは MISUMI の型番・単価・出荷日のプロパティを持つ。保存先に同じ型番のファイルがあれば取り込まない。

前提
  - SOLIDWORKS を通常の方法で起動してあること（スクリプトから自動起動した SOLIDWORKS は RAPiD Design から見えない）
  - RAPiD Design を起動してログイン済みであること（ツール > MiSUMi RAPiD Design > 起動。ログインは人が行う）
  - 実行中は RAPiD Design の窓を最前面にして操作する。ほかの窓が前に出たら、何も送らずに止まる
"""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

import pythoncom
import win32clipboard
import win32com.client
import win32gui
from win32com.client import VARIANT

from .config import PROGID

MAIN_TITLE = "ベースモデル検索画面"          # RAPiD Design の主窓の題名
HOME_BUTTON = (40, 70)                       # 主窓の左上にある HOME ボタン
SEARCH_BOX = (760, 174)                      # HOME 画面の検索欄の位置（主窓の左上から。窓の大きさに依らず左上基準）
PROPS = ("SpecNumber", "Maker", "Class", "UnitPrice", "ViaShipDate")


class RapidError(RuntimeError):
    pass


def _uia():
    from pywinauto import Desktop
    return Desktop(backend="uia")


def _main():
    ws = [w for w in _uia().windows() if (w.window_text() or "") == MAIN_TITLE]
    if not ws:
        raise RapidError("RAPiD Design の窓が無い。SOLIDWORKS の ツール > MiSUMi RAPiD Design > 起動 で開き、ログインしておく")
    return ws[0]


def _front(w):
    """窓を最前面にする。最前面にできなければ操作しない。"""
    for _ in range(3):
        try:
            w.set_focus()
        except Exception:
            pass
        time.sleep(0.4)
        fg = win32gui.GetForegroundWindow()
        if fg == w.handle or win32gui.GetAncestor(fg, 3) == w.handle:
            return
    raise RapidError("RAPiD Design の窓を最前面にできない（ほかの窓を操作中）。操作を送らずに中止した")


def _paste(text):
    """日本語入力の状態に左右されないよう貼り付けで入れる。クリップボードは元に戻す。"""
    from pywinauto import keyboard
    win32clipboard.OpenClipboard()
    try:
        has = win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT)
        old = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) if has else None
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()
    keyboard.send_keys("^v")
    time.sleep(0.4)
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        if old is not None:
            win32clipboard.SetClipboardText(old, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


def _button(w, label):
    for b in w.descendants(control_type="Button"):
        texts = [b.window_text() or ""] + [c.window_text() or "" for c in b.descendants(control_type="Text")]
        if label in [t.strip() for t in texts]:
            return b
    return None


def _close_design_windows():
    """取り込みのあとに出る「設計画面」の窓を閉じる（主窓の検索欄に重なるため）。"""
    for x in _uia().windows():
        t = x.window_text() or ""
        if t == "設計画面":
            try:
                x.close()
                time.sleep(1.0)
            except Exception:
                pass


def _home(w):
    """HOME 画面へ戻す（検索欄の位置を一定にするため）。"""
    from pywinauto import mouse
    r = w.rectangle()
    mouse.click(coords=(r.left + HOME_BUTTON[0], r.top + HOME_BUTTON[1]))
    time.sleep(3.0)


def _props(doc):
    ext = doc.Extension
    ext._FlagAsMethod("CustomPropertyManager")
    pm = ext.CustomPropertyManager("")
    pm._FlagAsMethod("Get")
    out = {}
    for n in pm.GetNames or ():
        if n in PROPS:
            out[n] = pm.Get(n)
    return out


def _sw():
    """通常起動した SOLIDWORKS へつなぐ。画面操作の直後は一時的に応答しないことがあるので、つなぎ直して待つ。"""
    last = None
    for _ in range(60):
        try:
            app = win32com.client.GetActiveObject(PROGID)
            app._FlagAsMethod("GetDocuments")
            app._FlagAsMethod("CloseDoc")
            app.GetDocuments()
            return app
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise RapidError(f"通常起動した SOLIDWORKS につなげない: {last}")


def fetch_one(spec, dest: Path):
    """型番 spec のモデルを取り込み、dest/MISUMI_<型番>.SLDPRT に保存する。プロパティの辞書を返す。"""
    from pywinauto import keyboard, mouse
    out = dest / f"MISUMI_{spec}.SLDPRT"
    # 前回の取り込みの後始末。先に「設計画面」を閉じてから、作業用の文書を閉じる
    # （設計画面がつながったままの文書を閉じると、RAPiD Design の処理が走って SOLIDWORKS がしばらく応答しなくなる）
    _close_design_windows()
    time.sleep(1.5)
    app = _sw()
    for d in app.GetDocuments() or ():
        if d.GetTitle.startswith("BaseModel"):
            app.CloseDoc(d.GetTitle)
            time.sleep(1.0)
    before = {d.GetTitle for d in (_sw().GetDocuments() or ())}
    btn = None
    for wait in (4.0, 8.0, 12.0):                     # 候補が出る前に Enter を押すと検索結果の画面へ行ってしまうので、待ちを延ばしてやり直す
        w = _main()
        _front(w)
        _home(w)
        _front(w)
        r = w.rectangle()
        mouse.click(coords=(r.left + SEARCH_BOX[0], r.top + SEARCH_BOX[1]))
        time.sleep(0.6)
        _front(w)
        keyboard.send_keys("^a{DELETE}")
        _paste(spec)
        time.sleep(wait)                              # 候補（型番ショートカット）が出るのを待つ
        _front(w)
        keyboard.send_keys("{UP}{ENTER}")             # 候補の最後が「型番ショートカット（商品詳細）」。上には検索履歴の候補が並ぶことがある
        for _ in range(8):                            # 「インポート」が出るまで待つ
            time.sleep(1.5)
            btn = _button(_main(), "インポート")
            if btn is not None:
                break
        if btn is not None:
            break
    if btn is None:
        raise RapidError(f"{spec}: 取り込みの確認画面が出ない（型番が無い、または CAD データが無い）")
    btn.invoke()
    doc = None
    for _ in range(60):                               # SOLIDWORKS にモデルが開くのを待つ
        time.sleep(2)
        app = _sw()
        for d in app.GetDocuments() or ():
            if d.GetTitle not in before and "BaseModel" in d.GetTitle:
                doc = d
        if doc is not None:
            break
    if doc is None:
        raise RapidError(f"{spec}: SOLIDWORKS にモデルが開かない")
    props = {}
    for _ in range(40):                               # RAPiD Design が型番などのプロパティを書き込むのを待つ
        time.sleep(1.5)
        try:
            props = _props(doc)
        except Exception:
            continue
        if props.get("SpecNumber") and props.get("UnitPrice"):
            break
    if props.get("SpecNumber") != spec:
        raise RapidError(f"{spec}: 開いたモデルの型番が違う（{props.get('SpecNumber')}）")
    e = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    wn = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    doc.Extension._FlagAsMethod("SaveAs")
    nothing = VARIANT(pythoncom.VT_DISPATCH, None)
    ok = doc.Extension.SaveAs(str(out), 0, 1 | 2, nothing, e, wn)       # 1 = 確認なし, 2 = 写しとして保存（元の文書は RAPiD のまま）
    if not ok or not out.exists():
        raise RapidError(f"{spec}: 保存できない err={e.value}")
    app.CloseDoc(doc.GetTitle)
    return props


def fetch(specs, dest):
    """型番のリストを取り込む。保存先に既にある型番は飛ばす。結果は dest/MISUMI_parts.csv に追記・更新する。"""
    dest = Path(dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    table = dest / "MISUMI_parts.csv"
    rows = {}
    if table.exists():
        rows = {r["SpecNumber"]: r for r in csv.DictReader(open(table, encoding="utf-8-sig"))}
    for spec in specs:
        if (dest / f"MISUMI_{spec}.SLDPRT").exists() and spec in rows:
            print(f"{spec}: 取り込み済み")
            continue
        p = fetch_one(spec, dest)
        rows[spec] = dict(p, 取り込み日=time.strftime("%Y-%m-%d"))
        print(f"{spec}: {p.get('Class', '')} 単価 {p.get('UnitPrice', '?')} 円 出荷 {p.get('ViaShipDate', '?')}")
        with open(table, "w", newline="", encoding="utf-8-sig") as f:
            wr = csv.DictWriter(f, fieldnames=list(PROPS) + ["取り込み日"])
            wr.writeheader()
            wr.writerows(rows.values())
    return rows


if __name__ == "__main__":
    try:
        fetch(sys.argv[2:], sys.argv[1])
    except RapidError as err:
        sys.exit(str(err))
