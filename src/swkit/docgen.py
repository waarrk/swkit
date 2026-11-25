"""Markdown の原稿から、顧客へ提出できる体裁の文書（Word と PDF）を作る。

    python -m swkit.docgen <原稿.md> [...]        同じ場所の ../09_release/（無ければ原稿の隣）へ <文書番号>_<版>.docx と .pdf を出す

原稿の先頭に TOML の文書情報を置く（+++ で囲む）。

    +++
    number = "UWD01-DDS-001"          # 文書番号
    title = "水中ドローン UWD-01 詳細設計書"
    version = "A"                     # 版
    date = "2026-11-25"
    author = "作成者"                  # 作成・審査・承認（空なら空欄）
    reviewer = ""
    approver = ""
    organization = ""                 # 発行元
    confidentiality = "社外秘"
    [[revision]]                      # 改訂履歴（表紙に載る）
    version = "A"
    date = "2026-11-25"
    content = "初版"
    author = "作成者"
    [[applicable]]                    # 適用文書（版のある文書だけ）
    number = "JIS B 2401-1"
    title = "Oリング"
    version = "2012"
    [[glossary]]                      # 用語集（付録に載る）
    term = "BG"
    meaning = "重心から浮心までの高さ"
    where = "4.2 節"
    +++

本文で使える書き方
    # 章 / ## 節 / ### 項            番号は自動で付く（1, 1.1, 1.1.1）
    段落、- 箇条書き、1. 番号付き
    | 表 | …                        直前の行に「表: 題名 {#tbl:名前}」
    ![題名](図のパス){#fig:名前}     図番号と題名が下に付く
    $$ 式 $$ {#eq:名前}              式番号が右に付く
    @fig:名前 @tbl:名前 @eq:名前      「図 3」「表 2」「式 (1)」に置き換わる

体裁: A4、表紙（題名・文書番号・版・日付・作成/審査/承認・改訂履歴）、目次（ページ番号付き）、適用文書、本文、付録の用語集、
全ページのヘッダに文書番号と版、フッタにページ番号。PDF は Word で書き出すので、目次のページ番号が埋まる。
"""
from __future__ import annotations

import csv
import re
import sys
import tomllib
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT  # noqa: F401
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor

MINCHO, GOTHIC = "游明朝", "游ゴシック"
BODY_PT, LINE_PT = 10.5, 18          # 行間は固定値（游明朝は倍数指定だと間延びする）


REGISTER = "文書管理台帳.csv"
COLUMNS = ["文書番号", "名称", "種別", "版", "発行日", "状態", "原稿", "提出物"]


class Register:
    """文書管理台帳。文書番号は台帳に登録してから使う。発行（docx・PDF の生成）のたびに版・発行日・提出物を書き戻す。"""

    def __init__(self, path: Path):
        """文書管理台帳を読む．"""
        self.path = path
        with open(path, encoding="utf-8-sig", newline="") as f:
            self.rows = list(csv.DictReader(f))

    @classmethod
    def find(cls, src: Path):
        """原稿の上位フォルダから文書管理台帳を探す．無ければ None．"""
        for up in src.parents:
            for d in up.glob("00_*"):
                if (d / REGISTER).exists():
                    return cls(d / REGISTER)
        return None

    def row(self, number):
        """文書番号の行を返す．"""
        return next((r for r in self.rows if r["文書番号"] == number), None)

    def check(self, meta, src):
        """原稿の文書番号・名称・版を台帳と照合し，合わなければ中止する．"""
        r = self.row(meta["number"])
        if r is None:
            raise SystemExit(f"{src.name}: 文書番号 {meta['number']} が {self.path.name} に無い。先に台帳へ登録する")
        if r["名称"] != meta["title"]:
            raise SystemExit(f"{src.name}: 名称が台帳と違う（台帳: {r['名称']}、原稿: {meta['title']}）")
        revs = [x["version"] for x in meta.get("revision", [])]
        if not revs or revs[-1] != meta["version"]:
            raise SystemExit(f"{src.name}: 改訂履歴の最後の版（{revs[-1] if revs else 'なし'}）が文書の版 {meta['version']} と違う")

    def issued(self, meta, src, out):
        """発行した版・発行日・状態・提出物の場所を台帳に書き戻す．"""
        r = self.row(meta["number"])
        base = self.path.parent.parent
        r.update({"版": meta["version"], "発行日": meta["date"], "状態": "発行" if meta.get("approver") else "審査前",
                  "原稿": src.relative_to(base).as_posix(), "提出物": out.relative_to(base).as_posix()})
        with open(self.path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(self.rows)


def parse(path: Path):
    """原稿を文書情報と本文に分ける．"""
    s = path.read_text(encoding="utf-8-sig")
    m = re.match(r"\+\+\+\n(.*?)\n\+\+\+\n", s, re.S)
    if not m:
        raise SystemExit(f"{path}: 先頭に +++ で囲んだ文書情報が無い")
    return tomllib.loads(m.group(1)), s[m.end():]


def _font(run, size=BODY_PT, bold=False, name=MINCHO):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = "Times New Roman" if name == MINCHO else "Arial"
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.append(rf)
    rf.set(qn("w:eastAsia"), name)


def _para(doc, text="", size=BODY_PT, bold=False, align=None, name=MINCHO, style=None, indent=0.0, space_after=4):
    p = doc.add_paragraph(style=style)
    pf = p.paragraph_format
    pf.line_spacing_rule, pf.line_spacing = WD_LINE_SPACING.EXACTLY, Pt(max(LINE_PT, size * 1.6))
    pf.space_before, pf.space_after = Pt(0), Pt(space_after)
    if indent:
        pf.left_indent = Mm(indent)
    if align is not None:
        p.alignment = align
    if text:
        _runs(p, text, size, bold, name)
    return p


def _runs(p, text, size=BODY_PT, bold=False, name=MINCHO):
    """`コード` は等幅に、$式$ は Word の数式に、ほかはそのまま。"""
    for i, part in enumerate(re.split(r"`([^`]*)`", text)):
        if not part:
            continue
        if i % 2:
            r = p.add_run(part)
            _font(r, size, bold, name)
            r.font.name = "Consolas"
            continue
        for k, seg in enumerate(re.split(r"\$([^$]+)\$", part)):
            if not seg:
                continue
            if k % 2:
                p._element.append(_omml(seg))
            else:
                _font(p.add_run(seg), size, bold, name)


def _field(p, code):
    """Word のフィールド（目次、ページ番号）。値は Word が開いたときに計算する。"""
    r = p.add_run()
    for kind, text in (("begin", None), (None, code), ("separate", None), ("end", None)):
        if kind:
            e = OxmlElement("w:fldChar")
            e.set(qn("w:fldCharType"), kind)
            if kind == "begin":
                e.set(qn("w:dirty"), "true")
        else:
            e = OxmlElement("w:instrText")
            e.set(qn("xml:space"), "preserve")
            e.text = text
        r._element.append(e)
    return r


GREEK = {"theta": "θ", "phi": "φ", "psi": "ψ", "rho": "ρ", "sigma": "σ", "pi": "π", "mu": "μ", "omega": "ω", "Delta": "Δ",
         "alpha": "α", "beta": "β", "gamma": "γ", "lambda": "λ", "tau": "τ", "times": "×", "cdot": "·", "le": "≤", "ge": "≥"}
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"


FUNCS = ("log", "ln", "exp", "sin", "cos", "tan", "max", "min", "sqrt")


def _omml(tex):
    """式の原稿を Word の数式に直す。

    使える書き方: \\mathrm{sin}（立体）、\\overline{BG}（上線）、\\theta などのギリシャ文字、\\log などの関数名（立体）、
    x_{ab} と x_0（下付き）、x^{2} と x^2（上付き）、\\,（空白）。
    """
    def el(tag, *kids, **attr):
        e = OxmlElement(f"m:{tag}")
        for k, v in attr.items():
            e.set(qn(f"m:{k}"), v)
        for k in kids:
            e.append(k)
        return e

    def run(text, upright=False):
        r = el("r")
        if upright:
            r.append(el("rPr", el("sty", val="p")))
        t = el("t")
        t.text = text
        t.set(qn("xml:space"), "preserve")
        r.append(t)
        return r

    tex = re.sub(r"\\([A-Za-z]+)", lambda m: GREEK.get(m.group(1), m.group(0)), tex).replace("\\,", " ")
    tok = re.compile(r"\\mathrm\{([^}]*)\}|\\overline\{([^}]*)\}|\\(" + "|".join(FUNCS) + r")(?![A-Za-z])|([_^])(?:\{([^}]*)\}|([A-Za-z0-9]+|.))|(\d+(?:\.\d+)?|[A-Za-z]|\s+|.)")
    atoms = []
    for m in tok.finditer(tex):
        if m.group(1) is not None:
            atoms.append(run(m.group(1), upright=True))
        elif m.group(2) is not None:
            atoms.append(el("bar", el("barPr", el("pos", val="top")), el("e", run(m.group(2)))))
        elif m.group(3) is not None:
            atoms.append(run(m.group(3), upright=True))
        elif m.group(4) is not None:                     # 直前の要素に添字を付ける
            script = m.group(5) if m.group(5) is not None else m.group(6)
            upright = bool(re.fullmatch(r"\d+(\.\d+)?", script)) or len(script) > 1
            base = atoms.pop() if atoms else run("")
            kind, pos = ("sSub", "sub") if m.group(4) == "_" else ("sSup", "sup")
            atoms.append(el(kind, el("e", base), el(pos, run(script, upright=upright))))
        else:
            text = m.group(7)
            atoms.append(run(text, upright=bool(re.fullmatch(r"\d+(\.\d+)?", text))))
    math = el("oMath")
    for a in atoms:
        math.append(a)
    return math


def _table(doc, rows, widths=None, header=True, size=9.5):
    t = doc.add_table(rows=len(rows), cols=len(rows[0]))
    t.style = "Table Grid"
    if widths is None:
        # 列幅は内容の長さに比例させる（全角 2、半角 1 と数える）。本文幅 166 mm に収める
        def w(x):
            return sum(2 if ord(c) > 0x2E7F else 1 for c in str(x))
        need = [min(max(w(r[j]) for r in rows), 44) + 3 for j in range(len(rows[0]))]
        widths = [166 * n / sum(need) for n in need]
        # 短い記号の列（番号、単位など）が途中で折り返さない最小幅を確保し、残りをほかの列で分ける
        floor = [min(w(max((str(r[j]) for r in rows), key=w)) * 1.9 + 5, 18) for j in range(len(rows[0]))]
        short = [j for j in range(len(widths)) if widths[j] < floor[j]]
        if short and len(short) < len(widths):
            rest = 166 - sum(floor[j] for j in short)
            tot = sum(widths[j] for j in range(len(widths)) if j not in short)
            widths = [floor[j] if j in short else widths[j] * rest / tot for j in range(len(widths))]
    t.autofit = False
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            c = t.cell(i, j)
            c.text = ""
            p = c.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            _runs(p, str(cell), size, bold=header and i == 0, name=GOTHIC if header and i == 0 else MINCHO)
            if header and i == 0:
                sh = OxmlElement("w:shd")
                sh.set(qn("w:val"), "clear")
                sh.set(qn("w:fill"), "E7E6E6")
                c._element.get_or_add_tcPr().append(sh)
            if widths:
                c.width = Mm(widths[j])
    return t


def _heading(doc, text, level):
    p = doc.add_paragraph(style=f"Heading {level}")
    p.paragraph_format.space_before, p.paragraph_format.space_after = Pt(10 if level == 1 else 6), Pt(4)
    r = p.add_run(text)
    _font(r, {1: 14, 2: 12, 3: 11}[level], True, GOTHIC)
    r.font.color.rgb = RGBColor(0, 0, 0)
    return p


def build(src: Path):
    """原稿から Word 文書を作り，保存先のパスを返す．文書管理台帳があれば照合と更新を行う．"""
    meta, body = parse(src)
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.left_margin = sec.right_margin = Mm(22)
    sec.top_margin, sec.bottom_margin = Mm(25), Mm(22)
    sec.different_first_page_header_footer = True
    hp = sec.header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _runs(hp, f"{meta['number']}  {meta['version']} 版" + (f"  {meta['confidentiality']}" if meta.get("confidentiality") else ""), 8.5, name=GOTHIC)
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _font(_field(fp, "PAGE"), 9)

    # ---- 表紙
    for _ in range(5):
        _para(doc)
    _para(doc, meta["title"], 22, True, WD_ALIGN_PARAGRAPH.CENTER, GOTHIC, space_after=18)
    _table(doc, [["文書番号", meta["number"]], ["版", meta["version"]], ["発行日", meta["date"]],
                 ["発行元", meta.get("organization", "")], ["機密区分", meta.get("confidentiality", "")]], [35, 90], header=False, size=10.5)
    _para(doc)
    _table(doc, [["作成", "審査", "承認"], [meta.get("author", ""), meta.get("reviewer", ""), meta.get("approver", "")]], [42, 42, 42], size=10.5)
    _para(doc)
    _para(doc, "改訂履歴", 11, True, name=GOTHIC)
    _table(doc, [["版", "日付", "改訂内容", "作成"]] + [[r["version"], r["date"], r["content"], r.get("author", "")] for r in meta.get("revision", [])],
           [14, 28, 96, 28])
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # ---- 目次
    _para(doc, "目次", 14, True, name=GOTHIC, space_after=8)
    _field(doc.add_paragraph(), 'TOC \\o "1-2" \\h \\z \\u')
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    # ---- 本文: 番号を振りながら 2 回読む（1 回目で図表・式の番号を決める）
    lines = body.split("\n")
    labels, n = {}, {"fig": 0, "tbl": 0, "eq": 0}
    for ln in lines:
        m = re.search(r"\{#(fig|tbl|eq):([\w\-]+)\}", ln)
        if m:
            n[m.group(1)] += 1
            labels[f"{m.group(1)}:{m.group(2)}"] = n[m.group(1)]
    names = {"fig": "図 {}", "tbl": "表 {}", "eq": "式 ({})"}

    def refs(t):
        def rep(m):
            key = f"{m.group(1)}:{m.group(2)}"
            if key not in labels:
                raise SystemExit(f"{src.name}: 参照先が無い @{key}")
            return names[m.group(1)].format(labels[key])
        return re.sub(r" ?@(fig|tbl|eq):([\w\-]+)", rep, t)

    h = [0, 0, 0]
    i, caption, appendix = 0, None, False
    while i < len(lines):
        ln = lines[i].rstrip()
        i += 1
        if not ln.strip():
            continue
        m = re.match(r"(#{1,3}) (.+)", ln)
        if m:
            lv = len(m.group(1))
            title = m.group(2).strip()
            if title.startswith("付録"):
                appendix = True
                _heading(doc, title, 1)
                continue
            h[lv - 1] += 1
            for k in range(lv, 3):
                h[k] = 0
            _heading(doc, ("付録 " if appendix else "") + ".".join(str(x) for x in h[:lv]) + "  " + refs(title), lv)
            continue
        m = re.match(r"表: (.+?)\s*\{#tbl:([\w\-]+)\}", ln)
        if m:
            caption = f"表 {labels['tbl:' + m.group(2)]}  {m.group(1)}"
            continue
        if ln.startswith("|"):
            rows = []
            i -= 1
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    rows.append([refs(c) for c in cells])
                i += 1
            if caption:
                _para(doc, caption, 9.5, True, WD_ALIGN_PARAGRAPH.CENTER, GOTHIC, space_after=2)
                caption = None
            _table(doc, rows)
            _para(doc, space_after=2)
            continue
        m = re.match(r"!\[(.*?)\]\((.+?)\)\s*\{#fig:([\w\-]+)\}(?:\{width=(\d+)\})?", ln)
        if m:
            img = (src.parent / m.group(2)).resolve()
            if not img.exists():
                raise SystemExit(f"{src.name}: 図が無い {img}")
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.add_run().add_picture(str(img), width=Mm(int(m.group(4) or 140)))
            _para(doc, f"図 {labels['fig:' + m.group(3)]}  {m.group(1)}", 9.5, True, WD_ALIGN_PARAGRAPH.CENTER, GOTHIC, space_after=8)
            continue
        m = re.match(r"\$\$\s*(.+?)\s*\$\$\s*\{#eq:([\w\-]+)\}", ln)
        if m:
            p = _para(doc, space_after=6)
            ts = p.paragraph_format.tab_stops
            ts.add_tab_stop(Mm(83), WD_TAB_ALIGNMENT.CENTER)
            ts.add_tab_stop(Mm(166), WD_TAB_ALIGNMENT.RIGHT)
            p.add_run("\t")
            p._element.append(_omml(m.group(1)))
            _font(p.add_run(f"\t({labels['eq:' + m.group(2)]})"), BODY_PT)
            continue
        m = re.match(r"(\s*)(?:[-*]|\d+\.) (.+)", ln)
        if m:
            depth = len(m.group(1)) // 2
            numbered = bool(re.match(r"\s*\d+\.", ln))
            p = _para(doc, style="List Number" if numbered else "List Bullet", indent=6 + 6 * depth, space_after=1)
            _runs(p, refs(m.group(2)))
            continue
        para = ln
        while i < len(lines) and lines[i].strip() and not re.match(r"(#|\||!\[|\$\$|\s*[-*] |\s*\d+\. |表: )", lines[i]):
            para += lines[i].strip()
            i += 1
        p = _para(doc, space_after=6)
        p.paragraph_format.first_line_indent = Mm(3.7)
        _runs(p, refs(para))

    # ---- 付録: 適用文書は本文側で書く。用語集は文書情報から作る
    if meta.get("glossary"):
        _heading(doc, "付録  用語集", 1)
        _table(doc, [["用語・記号", "意味", "定義の箇所"]] + [[g["term"], g["meaning"], g.get("where", "")] for g in meta["glossary"]], [38, 96, 32])

    reg = Register.find(src)
    out_dir = src.parent
    if reg is not None:
        reg.check(meta, src)
        rel = [d for d in reg.path.parent.parent.iterdir() if d.is_dir() and d.name.startswith("09_")]
        out_dir = rel[0] if rel else src.parent
    out = out_dir / f"{meta['number']}_{meta['version']}.docx"
    doc.save(out)
    if reg is not None:
        reg.issued(meta, src, out)
    return out


def to_pdf(docx: Path):
    """Word で開いて目次とページ番号を計算し、docx を保存し直して PDF を書き出す。"""
    import win32com.client
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    try:
        d = word.Documents.Open(str(docx))
        d.Fields.Update()
        for toc in d.TablesOfContents:
            toc.Update()
        d.Save()
        pdf = docx.with_suffix(".pdf")
        d.ExportAsFixedFormat(str(pdf), 17)
        pages = d.ComputeStatistics(2)
        d.Close(False)
    finally:
        word.Quit()
    return pdf, pages


def main():
    """コマンドラインから Word と PDF を生成する．"""
    for a in sys.argv[1:]:
        out = build(Path(a).resolve())
        pdf, pages = to_pdf(out)
        print(f"{out.name}  {pdf.name}  {pages} ページ")


if __name__ == "__main__":
    main()
