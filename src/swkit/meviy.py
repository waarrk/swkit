"""meviy（ミスミの加工品の見積りサービス）に加工品を入れ，作れるかどうかと見積りを読み取る．

    python -m swkit.meviy login                              ブラウザを開く．人がログインする
    python -m swkit.meviy upload <フォルダ名> <計画.csv>      フォルダを作り，計画の部品をアップロードする
    python -m swkit.meviy quote <フォルダ名> <計画.csv> [部品名 ...]   加工方法・材質を設定し，見積りを読む
    python -m swkit.meviy cautions <フォルダ名> <計画.csv> <部品名>    注意事項を 1 件ずつ開いて読む
    python -m swkit.meviy check <フォルダ名> <計画.csv> <部品名> <版の名前>   直した部品を版の名前で入れ，見積りと必要な寸法を読む

計画（CSV）の列: 部品，cad（部品ファイルの場所．CSV からの相対），数量，加工方法，材質の区分，材質，表面処理，備考．
金額は納期別・数量別にすべて result/<フォルダ名>/prices.csv に残し，quotes.csv には計画の数量での単価（数量スライド割引）と小計を出す．
結果は，計画と同じ場所の result/<フォルダ名>/quotes.csv と，部品ごとの画面の文字 <部品>.txt に残す．

守ること:
  - 注文しない．行うのはフォルダの作成，アップロード，見積条件の確定と読み取りまで．
  - 「この内容で見積依頼へ」は担当者への依頼になるので押さない．
  - ログインは人が行う．パスワードは扱わない．ログイン後の状態は専用のプロファイルに残して使い回す．
    場所は環境変数 MEVIY_PROFILE，無ければ ~/.swkit/meviy_profile．

速く終わらせるための段取り:
  - 固定の待ち時間を置かない．画面の状態が変わりしだい先へ進む（wait）．どこで何秒待ったかを最後に表示する．
  - 加工方法は，一覧で全部品を先に選ぶ（形状の認識は meviy 側で並行して進む）．
  - ビューアは部品ごとのタブで同時に開き，見積りの計算は全部品を先に走らせてから，まとめて読む．
"""
from __future__ import annotations

import csv
import os
import re
import sys
import time
import traceback
from pathlib import Path

BASE = "https://meviy.misumi-ec.com"
MYPAGE = BASE + "/ja-jp/app/fa/mypage"
PROFILE = Path(os.environ.get("MEVIY_PROFILE") or Path.home() / ".swkit" / "meviy_profile")
# 注文につながる操作．画面上の文言にこれらを含むボタンは押さない
FORBIDDEN = ("注文", "発注", "購入", "カート", "決済", "お支払", "見積依頼")
ALLOWED = ("自動見積開始", "見積条件を確定")
PRICE = re.compile(r"^1～\d+\s+[0-9,]+円", re.M)
ROW = "[class*=projectsListViewBodyRow]"
SIDE = "[class*=folderSideMenuTree-module__label]"       # 左のフォルダ一覧の名前
PROGRESS = re.compile(r"\d+% \(\d+/\d+\)")                # 送信中の行に出る進み具合
TABS = 4                       # 同時に開くビューアの数
KEYS = ["部品", "加工方法", "meviy品名", "材質", "表面処理(設定)", "状態", "単価", "出荷日", "型番", "数量", "数量での単価", "割引",
        "数量での出荷日", "小計", "納期別の単価", "注意事項", "備考"]
PRICE_KEYS = ["部品", "種別", "数量", "出荷日", "納期", "型番", "単価", "割引"]


class Waits:
    """待った場所と秒数の記録．遅い場所を見つけるために，最後に表にする．"""

    data = {}

    @classmethod
    def add(cls, what, dt, ok):
        rec = cls.data.setdefault(what, [0, 0.0, 0])
        rec[0] += 1
        rec[1] += dt
        rec[2] += 0 if ok else 1

    @classmethod
    def report(cls):
        if not cls.data:
            return
        print(f"\n■ 待ち時間の内訳\n{'待ったこと':<28}{'回数':>5}{'秒':>8}{'時間切れ':>8}")
        for k, (n, t, bad) in sorted(cls.data.items(), key=lambda kv: -kv[1][1]):
            print(f"{k:<28}{n:>5}{t:>8.1f}{bad:>8}")


def wait(page, cond, what, timeout_s=60, step_ms=250):
    """cond() が真になるまで待つ．固定の待ち時間を置かず，状態が変わりしだい先へ進む．時間切れなら False．"""
    t0 = time.time()
    ok = False
    while time.time() - t0 < timeout_s:
        try:
            if cond():
                ok = True
                break
        except Exception:
            pass
        page.wait_for_timeout(step_ms)
    Waits.add(what, time.time() - t0, ok)
    if not ok:
        print(f"  ! 時間切れ（{timeout_s} 秒）: {what}")
    return ok


def lines(page):
    """画面の文字を行ごとに返す．"""
    return [x.strip() for x in page.inner_text("body").splitlines() if x.strip()]


def visible(loc):
    return loc.count() > 0 and loc.first.is_visible()


def press(page, label):
    """見積りのためのボタンを押す．押してよいボタンは ALLOWED だけ．"""
    if label not in ALLOWED or any(w in label for w in FORBIDDEN):
        raise RuntimeError(f"押してはいけないボタン: {label}")
    btn = page.get_by_role("button", name=label)
    if visible(btn) and btn.first.is_enabled():
        btn.first.click()
        return True
    return False


# ------------------------------------------------------------ ブラウザ
def open_browser(p, headless=False):
    """インストール済みの Chrome を専用のプロファイルで開く．"""
    PROFILE.mkdir(parents=True, exist_ok=True)
    ctx = p.chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=headless, viewport={"width": 1500, "height": 950},
        accept_downloads=True, locale="ja-JP")
    return ctx, (ctx.pages[0] if ctx.pages else ctx.new_page())


def logged_in(page):
    return "meviy.misumi-ec.com" in page.url and "/app/" in page.url and "login" not in page.url.lower()


def start(p):
    """ブラウザを開き，マイページが使える状態になるまで待つ．ログインが切れていれば止める．"""
    ctx, page = open_browser(p)
    page.goto(MYPAGE, wait_until="domcontentloaded")
    ready = wait(page, lambda: "login" in page.url.lower() or visible(page.get_by_role("button", name="フォルダ新規作成")),
                 "マイページの表示", 40)
    if not ready or not logged_in(page):
        ctx.close()
        raise SystemExit("ログインが切れている．python -m swkit.meviy login を実行してログインし直す")
    return ctx, page


def login():
    """ブラウザを開き，人がログインを終えるまで待つ（最長 15 分）．"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = open_browser(p)
        page.goto(MYPAGE, wait_until="domcontentloaded")
        print("ブラウザを開いた．meviy にログインする（最長 15 分待つ）．")
        ok = wait(page, lambda: logged_in(page) and visible(page.get_by_role("button", name="フォルダ新規作成")), "人によるログイン", 900, 1000)
        print("ログインの確認:", "OK" if ok else "時間切れ")
        ctx.close()


def hide_overlays(page):
    """一覧の上にかぶさる小窓（アップロード完了の一覧，案内）を隠す．行の操作をふさぐため．"""
    page.evaluate("""() => {
        for (const key of ['個のアップロード完了', '自動見積もりの']) {
            const it = document.evaluate(`//*[contains(text(), '${key}')]`, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            for (let i = 0; i < it.snapshotLength; i++) {
                let e = it.snapshotItem(i);
                for (let k = 0; k < 12 && e; k++, e = e.parentElement) {
                    const pos = getComputedStyle(e).position;
                    if (pos === 'fixed' || pos === 'absolute') { e.style.display = 'none'; break; }
                }
            }
        }
    }""")


# ------------------------------------------------------------ フォルダと一覧
def folder_label(page, name):
    """左のフォルダ一覧にある name の行（同じ名前が複数あれば 1 つ目）．"""
    return page.locator(SIDE).get_by_text(name, exact=True)


def settled(page, selector, what, timeout_s=15, allow_empty=False):
    """selector に当たる要素の数が 1 秒変わらなくなるまで待つ．一覧は少しずつ出るので，出そろう前に数えない．"""
    loc, seen, same = page.locator(selector), -1, 0
    t0 = time.time()
    while time.time() - t0 < timeout_s and same < 4:
        n = loc.count()
        same = same + 1 if n == seen and (n > 0 or allow_empty) else 0
        seen = n
        page.wait_for_timeout(250)
    Waits.add(what, time.time() - t0, same >= 4)
    return seen


def folders_loaded(page):
    """左のフォルダ一覧が出そろうまで待つ．出る前に「無い」と判断すると，同じ名前のフォルダを作ってしまう．"""
    settled(page, SIDE, "フォルダ一覧の表示")


def ensure_folder(page, name):
    """フォルダが無ければ作る．マイページの直下には部品を置かない．"""
    folders_loaded(page)
    if folder_label(page, name).count() > 0:
        return False
    page.get_by_role("button", name="フォルダ新規作成").click()
    box = page.locator("input[class*=projectsDialog]")
    wait(page, lambda: visible(box), "フォルダ名の入力欄", 10)
    box.fill(name)
    page.get_by_role("button", name="作成", exact=True).click()
    wait(page, lambda: folder_label(page, name).count() > 0, "フォルダの作成", 20)
    print("フォルダを作成:", name)
    return True


def enter_folder(page, name, expect_rows=True):
    """フォルダに入り，一覧が出るまで待つ．"""
    folders_loaded(page)
    for _ in range(3):                                      # 画面の準備が終わる前に押すと効かない．URL がフォルダのものに変わるまで確かめる
        folder_label(page, name).first.click()
        if wait(page, lambda: "folderId=" in page.url, "フォルダへの移動", 6):
            break
    else:
        raise RuntimeError(f"フォルダに入れない: {name}")
    wait(page, lambda: visible(page.get_by_role("button", name="アップロード")), "フォルダの表示", 15)
    page.wait_for_timeout(500)                              # 上位の一覧の行が，フォルダの中の行に入れ替わる
    # 行は少しずつ出る．出そろう前に探すと，ある部品を「無い」と見誤る
    settled(page, ROW, "フォルダの中の一覧の表示", 30 if expect_rows else 4, allow_empty=not expect_rows)
    hide_overlays(page)


def row_of(page, file_name):
    return page.locator(ROW).filter(has_text=file_name).first


def has_viewer_link(page, file_name):
    return row_of(page, file_name).locator("a[href*='3dview']").count() > 0


def upload(page, folder, files):
    """フォルダに入り，まだ無い部品ファイルをアップロードして，一覧に出そろうまで待つ．"""
    made = ensure_folder(page, folder)
    enter_folder(page, folder, expect_rows=not made)
    todo = [f for f in files if row_of(page, Path(f).name).count() == 0]
    print(f"アップロード: {len(todo)} 件（既にある {len(files) - len(todo)} 件は入れ直さない）")
    if not todo:
        return
    page.get_by_role("button", name="アップロード").first.click()
    with page.expect_file_chooser() as fc:
        page.get_by_text("ファイルをアップロード", exact=True).click()
    fc.value.set_files([str(f) for f in todo])
    # 行は送信を始めた時点で一覧に出て，送信中は「30% (0/1)」のように進み具合が出る．この表示が消えたら送信は終わっている．
    # 「n 個のアップロード完了」の表示は前回までの分も残るので，合図に使えない
    # 行が出た直後は進み具合がまだ出ていない一瞬がある．そこで終わったと見誤ると，送信の途中で画面を離れてしまう．
    # 「進み具合が出ていない」状態が 3 秒続いたら終わりとする
    calm = [0]

    def sent():
        rows = [row_of(page, Path(f).name) for f in todo]
        ok = all(r.count() > 0 and not PROGRESS.search(r.inner_text()) for r in rows)
        calm[0] = calm[0] + 1 if ok else 0
        return calm[0] >= 6
    wait(page, sent, "アップロード", 420, 500)
    hide_overlays(page)


def set_method(page, file_name, method):
    """一覧で加工方法を合わせる．形状の認識が終わるのは待たない（あとでまとめて待つ）．

    アップロードの直後は行に「加工方法を選択してください」か「自動見積中」と出て，加工方法のボタンが並ぶ．
    押すまで先へ進まない．一覧の描き直しと重なると押しても効かないことがあるので，ボタンが消えたことを確かめて，残っていれば押し直す．
    """
    row = row_of(page, file_name)
    btn = row.get_by_role("button", name=method, exact=True)
    if visible(btn):
        for _ in range(3):
            btn.first.click()
            if wait(page, lambda: not visible(row_of(page, file_name).get_by_role("button", name=method, exact=True)),
                    "加工方法のボタンの反映", 6):
                return "選択"
        return "選択（ボタンが残ったまま）"
    cur = row.locator(".select__single-value").first
    if cur.count() and cur.inner_text().strip() != method:
        row.locator(".select__control").first.click()            # 選択肢は文字を打って Enter で選ぶ（一覧がほかの要素に隠れても選べる）
        page.wait_for_timeout(400)                               # 選択肢が開くのを待つ
        page.keyboard.type(method)
        page.wait_for_timeout(300)
        page.keyboard.press("Enter")
        return "変更"
    return "そのまま"


# ------------------------------------------------------------ ビューア（1 部品の見積り）
def viewer_ready(page):
    return any(x.startswith("品名") or x == "材質" for x in lines(page))


def close_notice(page):
    """ビューアを開いたときの小窓を閉じ，先へ進めない理由があればそれを返す（無ければ空）．

    小窓が出たままだと下のボタンを押せず，押そうとして時間切れまで待つことになる．
    """
    body = page.inner_text("body")
    why = ""
    if "サービス対象外の為" in body:
        why = "見積不可（サービス対象外の形状）"
    elif "自動見積中です" in body:
        m = re.search(r"最大\s*(\d+)\s*分", body)
        why = f"自動見積中（形状が複雑で，最大 {m.group(1) if m else '?'} 分かかる．あとで読み直す）"
    note = ""
    if "モデル修正案内" in body:                            # 曲げの R が板厚と合わないモデル．meviy 側で直した別のモデルは作らず，そのまま確かめる
        note = "曲げ R を認識できない箇所がある（外 R = 内 R + 板厚 になっていない）"
    for label in ("閉じる", "パーツビューを確認する", "モデルを修正しない"):
        ok = page.get_by_role("button", name=label, exact=True)
        if visible(ok):
            ok.first.click()
            page.wait_for_timeout(300)
            break
    if "曲げ画像確認" in body:
        # 板金の曲げの手順を絵で見せる小窓．曲げの途中で金型に当たる形のときに，開いた時点で出る．注意事項のボタンをふさぐので閉じる
        m = re.search(r"曲げ画像確認\s*(.+?[。．])", body.replace("\n", " "))
        note = (note + " ／ " if note else "") + "曲げの手順で金型に当たる: " + (m.group(1).strip() if m else "")
        try:
            page.locator("xpath=//*[normalize-space(text())='曲げ画像確認']/following-sibling::*[1]").first.click(timeout=2000)
        except Exception:
            page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    if note:
        print("  " + note)
    page.meviy_note = note
    return why


def set_method_in_viewer(page, method):
    """ビューアの加工方法を合わせる．変えたら，形状の認識が終わって品名が出直すまで待つ．"""
    sel = page.locator("select").filter(has=page.locator("option", has_text=re.compile("^切削$"))).first
    if not sel.count():
        return "選択ボックスなし"
    cur = sel.evaluate("e => e.options[e.selectedIndex].text").strip()
    if cur == method:
        return "そのまま"
    sel.select_option(label=method)
    page.wait_for_timeout(1000)
    wait(page, lambda: viewer_ready(page) and visible(page.get_by_text("材質・表面処理を変更", exact=True)), "加工方法の変更後の認識", 60)
    return f"{cur} → {method}"


def set_material(page, tab, material, surface_word):
    """材質と表面処理を選ぶ．選べた表面処理の名前を返す（選べなければ None）．"""
    change = page.get_by_role("button", name="材質・表面処理を変更")
    if not wait(page, lambda: visible(change) and change.first.is_enabled(), "材質の変更ボタン", 10):
        raise RuntimeError("「材質・表面処理を変更」を押せない（見積りできない状態か，小窓が出ている）")
    change.first.click()
    wait(page, lambda: "2. 表面処理を選ぶ" in lines(page), "材質の選択画面", 20)
    tab_loc = page.get_by_text(tab, exact=True)
    if not tab_loc.count():
        t = lines(page)
        i = next((k for k, x in enumerate(t) if x.startswith("1.")), 0)
        raise RuntimeError(f"材質の区分「{tab}」が無い．画面の候補: {' / '.join(t[i:i + 14])}")
    tab_loc.first.click()
    # 板金では「SUS304(2B)」「A5052(アルマイト鋼板黒)」のように，材質名のあとに仕上げや別の素材が付いた候補も並ぶ．
    # 同じ名前の候補があればそれを，無ければ前方一致のうちいちばん短い名前（素の材質）を選ぶ
    mat = page.get_by_text(re.compile("^" + re.escape(material)))
    if not wait(page, lambda: mat.count() > 0, "材質の一覧", 4):
        t = lines(page)
        i, j = (t.index(tab) if tab in t else 0), t.index("2. 表面処理を選ぶ")
        raise RuntimeError(f"材質「{material}」が無い．画面の候補: {' / '.join(dict.fromkeys(t[i:j]))}")
    names = [mat.nth(k).inner_text().strip() if mat.nth(k).is_visible() else "" for k in range(mat.count())]
    shown = [q for q in range(len(names)) if names[q]]
    if not shown:
        raise RuntimeError(f"材質「{material}」の候補が画面に出ていない")
    pick_name = material if material in names else min((names[q] for q in shown), key=len)
    k = max(q for q in shown if names[q] == pick_name)     # 同じ名前が小窓の外（いまの材質の表示）にもある．小窓の中はあとに出る
    print(f"  材質: {names[k]}（候補 {' / '.join(dict.fromkeys(n for n in names if n))}）")
    mat.nth(k).click()
    page.wait_for_timeout(800)                             # 材質を選ぶと表面処理の一覧が入れ替わる
    t = lines(page)
    i = t.index("2. 表面処理を選ぶ")
    j = len(t) - 1 - t[::-1].index("決定する")
    opts = [x for x in t[i + 1:j] if "詳しく" not in x and "担当者見積" not in x]
    pick = next((x for x in opts if surface_word and surface_word != "なし" and surface_word in x), None)
    if pick is not None:
        page.get_by_text(pick, exact=True).last.click()
        page.wait_for_timeout(300)
    elif surface_word and surface_word != "なし":
        print(f"  表面処理「{surface_word}」は選べない．候補: {' / '.join(dict.fromkeys(opts))}")
    page.get_by_role("button", name="決定する").click()
    wait(page, lambda: not visible(page.get_by_role("button", name="決定する")), "材質の反映", 30)
    return pick


def quote_state(page):
    """見積りの進み具合．'金額' / '対象外' / '不可' / '確定待ち' / '開始待ち' / '計算中'．"""
    body = page.inner_text("body")
    if PRICE.search(body):
        return "金額"
    if "見積もりできません" in body:                      # 画面の下に出る．待っても金額は出ない
        return "不可"
    if "見積依頼へ" in body or "担当者見積をご利用ください" in body:
        return "対象外"
    for label, state in (("見積条件を確定", "確定待ち"), ("自動見積開始", "開始待ち")):
        btn = page.get_by_role("button", name=label)
        if visible(btn) and btn.first.is_enabled():
            return state
    return "計算中"


def cautions_of(t):
    """画面の文字から注意事項の欄を抜き出す．"""
    if "注意事項" not in t:
        return []
    i, out = t.index("注意事項"), []
    for x in t[i + 1:i + 14]:                              # 注意事項の欄は「基本情報」で終わる．「確認」は各項目のボタン
        if x == "基本情報":
            break
        if x != "確認":
            out.append(x)
    return out


def parse(t):
    """画面の文字から 型番・単価（1 個）・出荷日・状態 を拾う．"""
    text = "\n".join(t)
    m = re.search(r"^1～\d+\s+([0-9,]+)円.*?(\d+)日目", text, re.M)
    # 型番は標準納期のもの．末尾に枝番が付いた型番も画面に出るので，いちばん短いものを採る
    pns = re.findall(r"MV[A-Z0-9]+(?:-[A-Z0-9]+){2,4}", text)
    pn = min(pns, key=len) if pns else ""
    if m:
        return pn, m.group(1).replace(",", ""), m.group(2), "自動見積"
    if "見積もりできません" in text:
        return "", "", "", "見積不可（このモデルのままでは見積りできない）"
    if "見積依頼へ" in text or "担当者見積をご利用ください" in text:
        return "", "", "", "自動見積の対象外（担当者見積）"
    return "", "", "", "金額が出ていない（画面の文字を確認）"


def prices_of(t):
    """画面の文字から，金額をすべて拾う．納期ごとの単価（特急・標準・長納期）と，数量ごとの単価．

    返り値: [{"種別": "納期別" | "数量別", "数量": 範囲, "出荷日": 日目, "納期": 標準納期などの表示, "型番": .., "単価": 円, "割引": ..}]
    納期別の単価は，画面で指定している数量（既定は 1）のときの値．
    """
    out = []
    for x in t:                                             # 数量スライド割引の表: 「2～10 7,603円 30%OFF 7日目」
        m = re.match(r"^(\d+～\d+)\s+([0-9,]+)円\s*(\d+%OFF)?\s*(\d+)日目", x)
        if m:
            out.append({"種別": "数量別", "数量": m.group(1), "出荷日": m.group(4), "納期": "", "型番": "",
                        "単価": m.group(2).replace(",", ""), "割引": m.group(3) or ""})
    qty = t[t.index("数量") + 1] if "数量" in t and t.index("数量") + 1 < len(t) and t[t.index("数量") + 1].isdigit() else "1"
    for i, x in enumerate(t):                               # 出荷日ごとの欄: 「7 日目 / 標準納期 / 型番 / 単価 / 10,856円」
        m = re.match(r"^(\d+)\s*日目$", x)
        if not m:
            continue
        blk = t[i + 1:i + 6]
        pn = next((b for b in blk if re.match(r"^MV[A-Z0-9]+-", b)), "")
        k = blk.index("単価") if "単価" in blk else -1
        if pn and 0 <= k < len(blk) - 1 and blk[k + 1].endswith("円"):
            out.append({"種別": "納期別", "数量": qty, "出荷日": m.group(1), "納期": "標準納期" if "標準納期" in blk[:2] else "",
                        "型番": pn, "単価": blk[k + 1].rstrip("円").replace(",", ""), "割引": ""})
    return out


def at_quantity(prices, qty):
    """数量 qty のときの単価・出荷日・小計を，数量スライド割引の表から求める．表の範囲を超える数量は None．"""
    for q in prices:
        if q["種別"] == "数量別":
            lo, hi = (int(v) for v in q["数量"].split("～"))
            if lo <= qty <= hi:
                return {"単価": int(q["単価"]), "出荷日": q["出荷日"], "割引": q["割引"], "小計": int(q["単価"]) * qty}
    return None


def quote(page, ctx, folder, plan, out, details=False):
    """計画の部品に加工方法と材質を設定し，見積りを読む．結果の一覧を返す．"""
    enter_folder(page, folder)
    names = {r["部品"]: Path(r["cad"]).name for r in plan}
    res = {r["部品"]: dict(r, 型番="", 単価="", 出荷日="", 状態="", 注意事項="") for r in plan}
    missing = [n for n, f in names.items() if row_of(page, f).count() == 0]
    for n in missing:
        res[n]["状態"] = "フォルダに無い（先に upload する）"
    todo = [r for r in plan if r["部品"] not in missing]
    # 1. 加工方法を全部品で先に選ぶ．見積り済み（型番が出ている）の部品は設定し直さない
    #    （同じ条件でも確定し直すと型番が再発行されて変わる）
    done = {r["部品"] for r in todo if re.search(r"MV[A-Z0-9]+-", row_of(page, names[r["部品"]]).inner_text())}
    for r in todo:
        if r["部品"] not in done:
            res[r["部品"]]["_how"] = set_method(page, names[r["部品"]], r["加工方法"])
    # 認識の終わった部品が増えているあいだは待つ．しばらく増えなければ，残りは置いて先へ進む（全部がそろうまで待ち続けない）．
    # 入れた直後の部品は認識に 1 分以上かかることがあるので，打ち切りは 120 秒
    t0, got, last = time.time(), -1, time.time()
    while time.time() - last < 120 and time.time() - t0 < 400:
        n = 0
        for r in todo:
            if has_viewer_link(page, names[r["部品"]]):
                n += 1
                continue
            # 入れた直後の行は，加工方法のボタンが遅れて出る．自動の判定（板金溶接など）に時間がかかっている途中でも，
            # ボタンが出ていれば計画の加工方法を押してよい．押さないと先へ進まない
            btn = row_of(page, names[r["部品"]]).get_by_role("button", name=r["加工方法"], exact=True)
            if visible(btn):
                try:
                    btn.first.click(timeout=2000)
                    last = time.time()
                    print(f"  加工方法を選択: {r['部品']} → {r['加工方法']}")
                except Exception:
                    pass
        if n != got:
            got, last = n, time.time()
            print(f"  形状の認識: {n}/{len(todo)} 点  [{time.time() - t0:.0f} 秒]")
        if n == len(todo):
            break
        page.wait_for_timeout(500)
    Waits.add("形状の認識（全部品）", time.time() - t0, got == len(todo))
    hrefs = {}
    for r in todo:
        link = row_of(page, names[r["部品"]]).locator("a[href*='3dview']")
        if link.count():
            hrefs[r["部品"]] = link.first.get_attribute("href")
        else:
            res[r["部品"]]["状態"] = "形状の認識が終わらない（一覧にビューアへのリンクが無い）"
    # 2. ビューアを部品ごとのタブで同時に開き，条件を入れて見積りを走らせ，まとめて読む
    queue = [r for r in todo if r["部品"] in hrefs]
    for k in range(0, len(queue), TABS):
        batch = queue[k:k + TABS]
        tabs = {}
        for r in batch:                                     # 先に全部のタブで読み込みを始める
            tabs[r["部品"]] = ctx.new_page()
            tabs[r["部品"]].goto(BASE + hrefs[r["部品"]], wait_until="commit")
        running = []
        for r in batch:
            part, tab = r["部品"], tabs[r["部品"]]
            try:
                tab.bring_to_front()
                tab.set_default_timeout(8000)               # 押せないボタンを 30 秒待たない
                wait(tab, lambda: viewer_ready(tab), "ビューアの表示", 90)
                tab.wait_for_timeout(700)                   # 小窓はビューアの表示より少し遅れて出る
                why = close_notice(tab)
                head = lines(tab)
                res[part]["meviy品名"] = next((x for x in head if x.startswith("品名")), "").replace("品名", "").strip()
                if why:
                    res[part]["_why"] = why                 # 条件を変えられない．画面を読むだけにする
                elif part in done or PRICE.search("\n".join(head)):
                    res[part]["表面処理(設定)"] = r["表面処理"]            # 見積り済み．読むだけ
                else:
                    how = set_method_in_viewer(tab, r["加工方法"])
                    if how not in ("そのまま", "選択ボックスなし"):
                        res[part]["meviy品名"] = next((x for x in lines(tab) if x.startswith("品名")), "").replace("品名", "").strip()
                    res[part]["表面処理(設定)"] = set_material(tab, r["材質の区分"], r["材質"], r["表面処理"]) or "なし"
                    wait(tab, lambda: quote_state(tab) != "計算中", "材質の反映後の準備", 30)
                    press(tab, "自動見積開始")
                running.append(part)
            except Exception as e:
                res[part]["状態"] = f"操作失敗: {type(e).__name__}: {str(e).splitlines()[0][:160]}"
                tb = traceback.extract_tb(e.__traceback__)
                print(f"{part}: {res[part]['状態']}\n   場所: " + " ← ".join(f"{f.name}:{f.lineno}" for f in tb[:3][::-1]))
        t0 = time.time()
        while running and time.time() - t0 < 240:           # 計算の終わった部品から順に確定して読む
            for part in list(running):
                tab = tabs[part]
                if res[part].get("_why"):
                    running.remove(part)
                    continue
                try:
                    st = quote_state(tab)
                    if st in ("開始待ち", "確定待ち"):
                        tab.bring_to_front()
                        press(tab, "自動見積開始" if st == "開始待ち" else "見積条件を確定")
                    elif st in ("金額", "対象外", "不可"):
                        running.remove(part)
                except Exception:
                    pass
            page.wait_for_timeout(500)
        Waits.add("見積りの計算（タブをまとめて）", time.time() - t0, not running)
        for r in batch:
            part, tab = r["部品"], tabs[r["部品"]]
            try:
                t = lines(tab)
                (out / f"{part}.txt").write_text("\n".join(t), encoding="utf-8")
                if not res[part]["状態"].startswith("操作失敗"):
                    res[part]["型番"], res[part]["単価"], res[part]["出荷日"], res[part]["状態"] = parse(t)
                    res[part]["_prices"] = prices_of(t)
                    n = int(r.get("数量") or 1)             # 計画の数量での単価（数量スライド割引を使う）
                    hit = at_quantity(res[part]["_prices"], n)
                    if hit:
                        res[part]["数量での単価"], res[part]["数量での出荷日"], res[part]["小計"] = hit["単価"], hit["出荷日"], hit["小計"]
                        res[part]["割引"] = hit["割引"]
                    elif res[part]["_prices"]:
                        res[part]["割引"] = "数量が表の範囲を超える（担当者見積）"
                    res[part]["納期別の単価"] = " / ".join(
                        f"{q['出荷日']}日目{'(標準)' if q['納期'] else ''} {q['単価']}円" for q in res[part]["_prices"] if q["種別"] == "納期別")
                    if res[part].get("_why"):
                        res[part]["状態"] = res[part]["_why"]
                res[part]["注意事項"] = "、".join(dict.fromkeys(cautions_of(t) + ([tab.meviy_note] if getattr(tab, "meviy_note", "") else [])))
                if details:
                    tab.bring_to_front()
                    res[part]["_details"] = caution_details(tab)
                tab.screenshot(path=str(out / f"{part}.png"))
            except Exception as e:
                res[part]["状態"] = res[part]["状態"] or f"読み取り失敗: {type(e).__name__}"
            tab.close()
            x = res[part]
            print(f"{part}: {x.get('meviy品名', '')} / {x['状態']} {x['単価']} 円 {x['出荷日']} 日目 {x['型番']}"
                  + (f"\n   注意事項: {x['注意事項']}" if x["注意事項"] else ""))
    return [res[r["部品"]] for r in plan]


def caution_details(page, out=None, stem=""):
    """開いているビューアで，注意事項を 1 件ずつ開いて説明の文を読む．[(見出し, 説明)] を返す．

    説明には，どの形がどれだけ足りないか（必要な寸法）が書かれていることが多い．形を直すときの手がかりにする．
    out を与えると，1 件ごとの画面も残す（該当する形に色が付く）．
    """
    heads = cautions_of(lines(page))
    base = set(lines(page))
    btns = page.get_by_role("button", name="確認", exact=True)
    found = []
    for i in range(btns.count()):
        try:
            btns.nth(i).click(timeout=4000)
        except Exception:
            continue
        page.wait_for_timeout(900)
        new = [x for x in lines(page) if x not in base and x not in ("閉じる", "キャンセル", "許容する", "許容しない")]
        found.append((heads[i] if i < len(heads) else f"注意 {i + 1}", " ".join(new[:10])))
        if out is not None:
            page.screenshot(path=str(out / f"{stem}_注意{i + 1}.png"))
        for lab in ("閉じる", "キャンセル"):
            c = page.get_by_role("button", name=lab, exact=True)
            if visible(c):
                c.first.click()
                page.wait_for_timeout(250)
                break
        if "曲げ画像確認" in page.inner_text("body"):       # 曲げの手順の絵は × で閉じる
            try:
                page.locator("xpath=//*[normalize-space(text())='曲げ画像確認']/following-sibling::*[1]").first.click(timeout=2000)
            except Exception:
                page.keyboard.press("Escape")
            page.wait_for_timeout(250)
    return found


NEED = re.compile(r"([^\s。、（(]{2,14}?)[（(]?\s*([0-9.]+)\s*mm[)）]?\s*(?:を|が).{0,60}?([0-9.]+)\s*mm\s*以上")
GUARANTEE = re.compile(r"([^\s。、]{2,14}?)[（(]\s*([0-9.]+)\s*mm[)）]が保証値を下回[^。]*。\s*保証値は\s*([0-9.]+)\s*mm")


def requirements(found):
    """注意事項の説明から「どの寸法を，いまの値から，いくつ以上にするか」を抜き出す．

    返り値: [{"kind": 区分, "what": 寸法の名前, "now": いまの値, "need": 必要な値, "text": 説明}]．
    区分は "確認事項"（直さないと見積りできない）か "品質確認"（許容すれば見積りできる）．数字の無い説明は what を空にして返す．
    """
    out = []
    for head, text in found:
        kind = "品質確認" if ("品質確認" in text or "保証値" in text) else "確認事項"     # 保証値を下回る注意は，許容すれば見積りできる
        m = GUARANTEE.search(text) or NEED.search(text)
        if m:
            out.append({"kind": kind, "what": m.group(1).strip("、。 "), "now": float(m.group(2)), "need": float(m.group(3)), "text": text})
        else:
            out.append({"kind": kind, "what": "", "now": None, "need": None, "text": (head + " " + text).strip()})
    return out


def check(page, ctx, folder, row, file, name, out):
    """1 部品を名前 name でアップロードし，見積りと注意事項の詳細を読む．形を直しながら繰り返し確かめるための 1 回分．

    同じ部品を直して何度も確かめるので，名前には版（例: ブラケット_r2）を付ける．結果は out/<name>.json にも残す．
    """
    import json
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / f"{name}{Path(file).suffix}"
    shutil.copy2(file, tmp)
    upload(page, folder, [tmp])
    page.goto(MYPAGE, wait_until="domcontentloaded")
    res = quote(page, ctx, folder, [dict(row, 部品=name, cad=str(tmp))], out, details=True)[0]
    res["要求"] = requirements(res.pop("_details", []))
    res["金額"] = res.get("_prices", [])
    (out / f"{name}.json").write_text(json.dumps({k: v for k, v in res.items() if not k.startswith("_")}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    return res


def read_cautions(page, folder, file_name, out, stem):
    """部品の注意事項を 1 件ずつ開いて，説明の文と画面を残す．"""
    enter_folder(page, folder)
    href = row_of(page, file_name).locator("a[href*='3dview']").first.get_attribute("href")
    page.goto(BASE + href, wait_until="domcontentloaded")
    wait(page, lambda: viewer_ready(page), "ビューアの表示", 90)
    page.wait_for_timeout(700)
    close_notice(page)
    found = caution_details(page, out, stem)
    (out / f"{stem}_注意.txt").write_text("\n".join(f"[{h}] {d}" for h, d in found), encoding="utf-8")
    for h, d in found:
        print(f"[{h}]\n    {d[:400]}")
    return found


# ------------------------------------------------------------ コマンド
def read_plan(path, only=()):
    path = Path(path)
    rows = [r for r in csv.DictReader(open(path, encoding="utf-8-sig")) if not only or r["部品"] in only]
    for r in rows:
        r["cad"] = str((path.parent / r["cad"]).resolve())
    return rows


def save(path, results, partial):
    old = {}
    if path.exists() and partial:                          # 一部だけやり直したときは，ほかの行を残す
        old = {r["部品"]: r for r in csv.DictReader(open(path, encoding="utf-8-sig"))}
    for r in results:
        old[r["部品"]] = {k: r.get(k, "") for k in KEYS}
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=KEYS)
        w.writeheader()
        w.writerows(old.values())
    print(path)
    # 金額は納期別・数量別にすべて残す（1 行 = 1 つの金額）
    ppath = path.with_name("prices.csv")
    keep = []
    if ppath.exists() and partial:
        redo = {r["部品"] for r in results}
        keep = [r for r in csv.DictReader(open(ppath, encoding="utf-8-sig")) if r["部品"] not in redo]
    rows = keep + [dict(q, 部品=r["部品"]) for r in results for q in r.get("_prices", [])]
    with open(ppath, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=PRICE_KEYS)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in PRICE_KEYS} for r in rows)
    print(ppath, len(rows), "行")
    total = sum(int(r["小計"]) for r in old.values() if str(r.get("小計", "")).isdigit())
    n_ok = sum(1 for r in old.values() if str(r.get("小計", "")).isdigit())
    print(f"計画の数量での合計: {total:,} 円（税別．見積りの出た {n_ok} 点／全 {len(old)} 点）")


def main(argv=None):
    """コマンドラインの入口．"""
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] not in ("login", "upload", "quote", "cautions", "check"):
        print(__doc__)
        return 1
    if a[0] == "login":
        login()
        return 0
    from playwright.sync_api import sync_playwright
    folder, plan_path, only = a[1], Path(a[2]), (a[3:4] if a[0] == "check" else a[3:])
    plan = read_plan(plan_path, only)
    out = plan_path.parent / "result" / folder
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with sync_playwright() as p:
        ctx, page = start(p)
        try:
            if a[0] == "upload":
                lost = [r["cad"] for r in plan if not Path(r["cad"]).exists()]
                if lost:
                    raise SystemExit("部品ファイルが無い: " + "、".join(lost))
                upload(page, folder, [r["cad"] for r in plan])
            elif a[0] == "check":                           # check <フォルダ名> <計画.csv> <部品名> <アップロードする名前>
                r = check(page, ctx, folder, plan[0], plan[0]["cad"], a[4], out)
                print(f"{a[4]}: {r['状態']} {r['単価']} 円 {r['出荷日']} 日目")
                for q in r["要求"]:
                    print(f"  [{q['kind']}] " + (f"{q['what']} {q['now']:g} → {q['need']:g} mm 以上" if q["what"] else q["text"][:150]))
            elif a[0] == "quote":
                save(out / "quotes.csv", quote(page, ctx, folder, plan, out), bool(only))
            else:
                read_cautions(page, folder, Path(plan[0]["cad"]).name, out, plan[0]["部品"])
        finally:
            ctx.close()
    Waits.report()
    print(f"全体 {time.time() - t0:.0f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
