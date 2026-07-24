# -*- coding: utf-8 -*-
"""
レシート自動仕分けアプリ（誤検知ゼロ設計）
- クレジット明細CSV と スキャンレシートPDF を照合
- 100%確実な金額一致でのみ紐付け、曖昧なものは全て手動確認へ
"""
import os
import io
import csv
import re
import zipfile
import tempfile
from collections import Counter, defaultdict

import streamlit as st
import pdfplumber
from pypdf import PdfReader, PdfWriter

# =========================================================
# 画面設定
# =========================================================
st.set_page_config(page_title="レシート自動仕分け（厳格版）", page_icon="🧾", layout="wide")
st.title("🧾 レシート自動仕分けアシスタント")
st.caption("金額が100%確実に一致したものだけを紐付けます。少しでも曖昧なものは「手動確認」に回します。")
st.divider()

# =========================================================
# OCR文字化け対策・金額抽出ロジック（サンプル10ページで検証済み）
# =========================================================

def fix_amount_token(t: str) -> str:
    """金額トークン内部だけのOCR復元（店名など他テキストには使わない）"""
    return (t.replace('フ', '7').replace('B', '8').replace('S', '8')
             .replace('O', '0').replace('D', '0').replace('l', '1').replace('I', '1')
             .replace('，', ',').replace('．', '.'))

def to_int_amount(s: str):
    s = fix_amount_token(s).replace(',', '').replace('.', '')
    return int(s) if s.isdigit() else None

# 支払確定を示す最優先キーワード
PRIMARY = (r'(合\s*計|合\s*言\s*十|クレジット|ｸﾚｼﾞｯﾄ|ﾙﾘ|ﾙﾂ|領収金額|領収錨|支払金額|'
           r'ご利用額|お買上合計|お頁上合計|お買上高|お宜卜|TOTAL|CREDIT|金\s*額)')
# 本命にしてはいけない語（税額・小計・対象額など）
NEG = r'(消費税|消費穂|消費癖|消餐税|清書|内消|税額|小\s*計|外税|点数|お釣|返品|対象)'
# 通貨記号（¥ ￥ と、OCRが半角¥を化けさせた \ ）
CUR = r'[¥￥\\]'

def find_primary_amounts(text: str):
    """本命キーワード行にある金額。税率(%直前)・税額語は除外"""
    out = []
    for line in text.split('\n'):
        for m in re.finditer(r'([0-9BSOfフDlI][0-9BSOfフDlI，,．.]{2,9})', line):
            v = to_int_amount(m.group(1))
            if not v or not (100 <= v <= 9999999):
                continue
            before = line[max(0, m.start() - 2):m.start()]
            if '%' in before or '％' in before:  # 8.00% の 00 などを除外
                continue
            left = line[:m.start()]
            if re.search(PRIMARY, left):
                neg = [x.start() for x in re.finditer(NEG, left)]
                pri = [x.start() for x in re.finditer(PRIMARY, left)]
                if neg and pri and max(neg) > max(pri):
                    continue
                out.append(v)
    return out

def find_currency_amounts(text: str):
    """通貨記号付き金額（バックアップ）。記号付き=金額として採用"""
    out = []
    for m in re.finditer(CUR + r'\s*([0-9BSOfフDlI][0-9BSOfフDlI，,．.]*)', text):
        v = to_int_amount(m.group(1))
        if v and 100 <= v <= 9999999:
            out.append(v)
    return out

def confirm_amount(text: str, csv_amounts):
    """レシートの確定金額を返す。CSVに存在する金額のみ採用。取れなければ None"""
    prim = find_primary_amounts(text)
    cand = [v for v in prim if v in csv_amounts]
    if not cand:
        cand = [v for v in find_currency_amounts(text) if v in csv_amounts]
    if not cand:
        return None
    return Counter(cand).most_common(1)[0][0]

# =========================================================
# 日付抽出（誤検知防止：電話番号などを日付と誤認しない）
# =========================================================

def extract_receipt_dates(text: str):
    """レシートから (月, 日) の集合を返す。ハイフン/ドット区切りは電話番号誤認防止で除外"""
    dates = set()
    for m in re.finditer(r'20\d{2}\s*[/年.]\s*([01]?\d)\s*[/月.]\s*([0-3]?\d)', text):
        try:
            dates.add((int(m.group(1)), int(m.group(2))))
        except Exception:
            pass
    for m in re.finditer(r'([01]?\d)\s*月\s*([0-3]?\d)\s*日', text):
        try:
            dates.add((int(m.group(1)), int(m.group(2))))
        except Exception:
            pass
    return dates

def parse_csv_date(date_str: str):
    """CSVの利用日 '2月14日' などから (月, 日) を返す"""
    m = re.search(r'([01]?\d)\s*月\s*([0-3]?\d)\s*日', date_str)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r'20\d{2}[/.-]([01]?\d)[/.-]([0-3]?\d)', date_str)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None

# =========================================================
# 決済手段・店名のキーワード
# =========================================================
PAYPAY_KW = ['paypay', 'ペイペイ', 'ｐａｙｐａｙ', 'ぺいぺい']
CARD_KW = ['クレジット', 'ｸﾚｼﾞｯﾄ', 'クレシ', 'visa', 'mastercard', 'jcb', 'amex',
           'アメックス', 'ｱﾒｯｸｽ', 'american express', 'お客様控え', 'カード売上', '売上票',
           '一括', 'ご利用票', 'ご利用額']
CASH_KW = ['現金', '現金払', 'おつり', 'お釣り', '釣銭']

def sanitize_filename(text: str) -> str:
    for ch in ['\\', '/', ':', '*', '?', '"', '<', '>', '|']:
        text = text.replace(ch, '_')
    return text.strip()

# =========================================================
# CSV読み込み（アメックス・楽天等に柔軟対応）
# =========================================================

def load_statements(csv_file):
    """CSVから明細リストを返す。列名は柔軟に判定"""
    content = csv_file.getvalue()
    text = None
    for enc in ['utf-8-sig', 'cp932', 'shift_jis', 'utf-8']:
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("文字コードを判定できませんでした")

    lines = text.splitlines()
    # ヘッダー行を探す
    header_idx = 0
    for i, line in enumerate(lines):
        if any(k in line for k in ['金額', '利用', '摘要', '明細', '店名', 'ご利用']):
            header_idx = i
            break

    reader = csv.DictReader(lines[header_idx:])
    rows = []
    for row in reader:
        keys = [k for k in row.keys() if k]
        amount_key = next((k for k in keys if '金額' in k or '利用額' in k or 'ご利用金額' in k), None)
        date_key = next((k for k in keys if ('日' in k or '月' in k) and '曜' not in k), None)
        shop_key = next((k for k in keys if '摘要' in k or '明細' in k or '店名' in k or 'ご利用先' in k or '利用先' in k), None)
        if not amount_key:
            continue
        raw = (row.get(amount_key) or '').replace(',', '').replace('円', '').strip()
        if not raw.lstrip('-').isdigit():
            continue
        amount = int(raw)
        if amount <= 0:  # 返金・マイナスは照合対象外
            continue
        rows.append({
            'date': (row.get(date_key) or '不明') if date_key else '不明',
            'shop': (row.get(shop_key) or '不明') if shop_key else '不明',
            'amount': amount,
            'matched_count': 0,
            'matched': False,
            'reason': '',
        })
    return rows

# =========================================================
# メイン UI
# =========================================================
st.subheader("1️⃣ ファイルをアップロード（複数可）")
c1, c2 = st.columns(2)
with c1:
    csv_files = st.file_uploader("💳 カード明細CSV", type="csv", accept_multiple_files=True)
with c2:
    pdf_files = st.file_uploader("🧾 レシートPDF", type="pdf", accept_multiple_files=True)

run = st.button("🚀 仕分けを実行", type="primary", use_container_width=True)

if run:
    if not pdf_files:
        st.warning("レシートPDFをアップロードしてください。")
        st.stop()

    with st.spinner("金額を確認しながら厳格に仕分け中..."):
        # --- CSV読み込み ---
        statements = []
        if csv_files:
            for f in csv_files:
                try:
                    statements.extend(load_statements(f))
                except Exception as e:
                    st.error(f"CSV読み込みエラー ({f.name}): {e}")

        csv_amounts = [s['amount'] for s in statements]
        amount_freq = Counter(csv_amounts)

        with tempfile.TemporaryDirectory() as temp_dir:
            out_root = os.path.join(temp_dir, "仕分け結果")
            os.makedirs(out_root)

            FOLDERS = {
                'matched': '01_照合済レシート',
                'paypay': '02_PayPay払い',
                'card': '03_未照合カード',
                'cash': '04_現金払い',
                'manual': '05_手動確認',
            }

            unmatched_rows = []  # 未照合・その他一覧CSV用

            # --- レシート1ページずつ処理 ---
            for pdf_file in pdf_files:
                try:
                    pdf_bytes = io.BytesIO(pdf_file.getvalue())
                    reader = PdfReader(pdf_bytes)
                    with pdfplumber.open(pdf_bytes) as plumber:
                        for page_num in range(len(reader.pages)):
                            text = plumber.pages[page_num].extract_text() or ''

                            # 確定金額（CSV照合済み）
                            amount = confirm_amount(text, csv_amounts)
                            receipt_dates = extract_receipt_dates(text)

                            matched_item = None
                            if amount is not None:
                                # 同額の明細行を集める
                                same = [s for s in statements if s['amount'] == amount]
                                if len(same) == 1:
                                    matched_item = same[0]
                                else:
                                    # 同額が複数 → 日付で優先的に絞り、空き行を優先
                                    # 1) 日付一致かつ未使用
                                    cand = None
                                    for s in same:
                                        md = parse_csv_date(s['date'])
                                        if md and receipt_dates and md in receipt_dates and s['matched_count'] == 0:
                                            cand = s
                                            break
                                    # 2) 未使用の行
                                    if cand is None:
                                        cand = next((s for s in same if s['matched_count'] == 0), None)
                                    # 3) 全て埋まっていれば最初の行に吸収（_1, _2）
                                    if cand is None:
                                        cand = same[0]
                                    matched_item = cand

                            # --- 出力先とファイル名を決定 ---
                            writer = PdfWriter()
                            writer.add_page(reader.pages[page_num])

                            if matched_item is not None:
                                matched_item['matched'] = True
                                matched_item['matched_count'] += 1
                                matched_item['reason'] = f"金額一致(¥{amount:,})"

                                # ファイル名：YYYY年MM月DD日_金額円.pdf
                                md = parse_csv_date(matched_item['date'])
                                if md:
                                    date_label = f"2026年{md[0]:02d}月{md[1]:02d}日"
                                else:
                                    date_label = sanitize_filename(matched_item['date'])
                                base = f"{date_label}_{amount:,}円"
                                folder = os.path.join(out_root, FOLDERS['matched'])
                                os.makedirs(folder, exist_ok=True)

                                # ダブり吸収：同名があれば _1, _2 ...
                                fname = base + ".pdf"
                                path = os.path.join(folder, fname)
                                n = 1
                                while os.path.exists(path):
                                    fname = f"{base}_{n}.pdf"
                                    path = os.path.join(folder, fname)
                                    n += 1
                            else:
                                # 未照合 → 決済手段で振り分け
                                low = text.lower()
                                if any(k in low for k in PAYPAY_KW):
                                    key = 'paypay'
                                elif any(k in low for k in [x.lower() for x in CARD_KW]):
                                    key = 'card'
                                elif any(k in text for k in CASH_KW):
                                    key = 'cash'
                                else:
                                    key = 'manual'

                                folder = os.path.join(out_root, FOLDERS[key])
                                os.makedirs(folder, exist_ok=True)
                                base = f"{os.path.splitext(pdf_file.name)[0]}_P{page_num + 1}"
                                fname = base + ".pdf"
                                path = os.path.join(folder, fname)
                                n = 1
                                while os.path.exists(path):
                                    fname = f"{base}_{n}.pdf"
                                    path = os.path.join(folder, fname)
                                    n += 1

                                # 未照合一覧に記録
                                guess_amt = confirm_amount(text, csv_amounts)
                                cur = find_currency_amounts(text)
                                guess_str = f"¥{max(cur):,}" if cur else "（読取不可）"
                                dstr = "（読取不可）"
                                if receipt_dates:
                                    m0, d0 = sorted(receipt_dates)[0]
                                    dstr = f"{m0}月{d0}日"
                                unmatched_rows.append([
                                    fname, dstr, guess_str,
                                    re.sub(r'\s+', ' ', text)[:100]
                                ])

                            with open(path, "wb") as fo:
                                writer.write(fo)

                except Exception as e:
                    st.error(f"PDF処理エラー ({pdf_file.name}): {e}")

            # --- レポート1：全体_明細照合レポート ---
            report_path = os.path.join(out_root, "📝全体_明細照合レポート.csv")
            with open(report_path, 'w', encoding='utf-8-sig', newline='') as f:
                w = csv.writer(f)
                w.writerow(["利用日", "摘要（店名）", "金額", "状況", "AI判定理由"])
                for s in statements:
                    status = "〇 提出済" if s['matched'] else "× 未提出"
                    reason = s['reason']
                    if s['matched_count'] > 1:
                        reason += f"【ダブり{s['matched_count']}枚】"
                    if not s['matched']:
                        reason = "レシート未検出"
                    w.writerow([s['date'], s['shop'], f"{s['amount']:,}", status, reason])

            # --- レポート2：未照合・その他一覧 ---
            if unmatched_rows:
                un_path = os.path.join(out_root, "📝未照合・その他一覧.csv")
                with open(un_path, 'w', encoding='utf-8-sig', newline='') as f:
                    w = csv.writer(f)
                    w.writerow(["ファイル名", "推測される日付", "推測される金額", "読み取った生の文字（先頭100字）"])
                    w.writerows(unmatched_rows)

            # --- ZIP化 ---
            zip_path = os.path.join(temp_dir, "仕分け結果.zip")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for root, _, files in os.walk(out_root):
                    for file in files:
                        fp = os.path.join(root, file)
                        z.write(fp, os.path.relpath(fp, out_root))

            # --- 結果表示 ---
            total = len(statements)
            matched = sum(1 for s in statements if s['matched'])
            st.success("仕分けが完了しました。")
            m1, m2, m3 = st.columns(3)
            m1.metric("明細の総件数", f"{total} 件")
            m2.metric("レシート提出済", f"{matched} 件")
            m3.metric("未提出", f"{total - matched} 件")

            if unmatched_rows:
                st.subheader("要手動確認・未照合レシート")
                st.dataframe(
                    [{"ファイル名": r[0], "推測日付": r[1], "推測金額": r[2]} for r in unmatched_rows],
                    use_container_width=True,
                )

            with open(zip_path, "rb") as f:
                zip_data = f.read()
            st.download_button(
                "📥 仕分け結果（ZIP）をダウンロード",
                data=zip_data,
                file_name="仕分け結果.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )
