import streamlit as st
import os
import csv
import pdfplumber
from pypdf import PdfReader, PdfWriter
import zipfile
import tempfile
import io

# 🐻 画面の基本設定（動物モチーフ＆正確性重視）
st.set_page_config(page_title="レシート自動仕分けアシスタント", page_icon="🐻", layout="centered")

st.title("🐻 事務所専用：レシート自動仕分けアシスタント")
st.markdown("明細CSVとレシートPDFを照合し、正確にフォルダ分けを行います。🐾")
st.divider()

# タブで機能を分ける
tab1, tab2 = st.tabs(["🐰 レシート自動仕分け", "🐱 【補助機能】明細PDF→CSV変換"])

# --- 共通のキーワード設定 ---
CARD_KEYWORDS = ["クレジット", "visa", "mastercard", "jcb", "amex", "ｸﾚｼﾞｯﾄ", "一括", "お客様控え", "クレ電子", "アメリカン", "カード売"]
CASH_KEYWORDS = ["現金", "お預り", "お釣り", "お預かり"]
PAYPAY_KEYWORDS = ["paypay", "ペイペイ", "ｐａｙｐａｙ"]

def sanitize_filename(text):
    invalid_chars = ['\\', '/', ':', '*', '?', '"', '<', '>', '|']
    for ch in invalid_chars: text = text.replace(ch, '_')
    return text

def extract_purchased_items(text):
    lines = text.split('\n')
    items = []
    ignore_words = ['計', '税', 'お釣り', 'クレジット', '対象', '点数', '現金', '支払', '承認', '番号', '会員', 'カード', '割引', 'No', 'レジ', '売上', '合', '外']
    for line in lines:
        if '¥' in line or '円' in line or '*' in line:
            if not any(ignore in line for ignore in ignore_words):
                clean_line = line.replace('¥', '').replace('*', '').strip()
                if len(clean_line) > 2 and not clean_line.replace(',', '').isdigit():
                    items.append(clean_line[:15])
    if items:
        return "、".join(items[:2])
    return "（PDFを確認）"

# ==========================================
# タブ1：レシート自動仕分け機能
# ==========================================
with tab1:
    st.subheader("1️⃣ 照合するデータをセットしてください")
    col1, col2 = st.columns(2)
    with col1:
        csv_files = st.file_uploader("💳 カード明細（CSV）", type="csv", accept_multiple_files=True)
    with col2:
        pdf_files = st.file_uploader("🧾 レシート（PDF）", type="pdf", accept_multiple_files=True)

    if st.button("🐾 正確に仕分けを開始する", use_container_width=True, type="primary"):
        if not pdf_files:
            st.warning("⚠️ レシートPDFがアップロードされていません。")
        else:
            with st.spinner('🐻 データを正確に照合しています...'):
                with tempfile.TemporaryDirectory() as temp_dir:
                    output_dir = os.path.join(temp_dir, "03_仕分け結果")
                    os.makedirs(output_dir)
                    
                    statements = []
                    unmatched_list = [] # 未照合カードの一覧用リスト
                    
                    # CSVの読み込み
                    if csv_files:
                        for csv_file in csv_files:
                            folder_name = f"01_{os.path.splitext(csv_file.name)[0]}"
                            try:
                                decoded_file = csv_file.getvalue().decode('utf-8-sig').splitlines()
                                reader = csv.DictReader(decoded_file)
                                for row in reader:
                                    amount_key = next((k for k in row.keys() if k and '金額' in k), None)
                                    date_key = next((k for k in row.keys() if k and '日' in k), None)
                                    shop_key = next((k for k in row.keys() if k and '摘要' in k), None)
                                    
                                    if amount_key and row.get(amount_key):
                                        amount_str = row[amount_key].replace(',', '').strip()
                                        date_str = row.get(date_key, '不明') if date_key else '不明'
                                        shop_str = row.get(shop_key, '不明') if shop_key else '不明'
                                        
                                        if amount_str.isdigit():
                                            statements.append({
                                                'card_name': folder_name,
                                                'date': date_str,
                                                'shop': shop_str,
                                                'amount': amount_str,
                                                'amount_comma': f"{int(amount_str):,}",
                                                'matched': False,
                                                'summary': "未抽出"
                                            })
                            except Exception as e:
                                st.error(f"CSV読み込みエラー: {e}")

                    # PDFの処理
                    for pdf_file in pdf_files:
                        try:
                            pdf_bytes = io.BytesIO(pdf_file.getvalue())
                            reader = PdfReader(pdf_bytes)
                            with pdfplumber.open(pdf_bytes) as pdf_text:
                                for page_num in range(len(reader.pages)):
                                    extracted = pdf_text.pages[page_num].extract_text()
                                    text = extracted if extracted else ""
                                    text_norm = text.lower().replace(" ", "").replace(" ", "").replace("\n", "")
                                    
                                    matched_card = None
                                    matched_info = None
                                    
                                    for item in statements:
                                        if not item['matched']:
                                            if item['amount'] in text_norm or item['amount_comma'] in text_norm:
                                                item['matched'] = True
                                                matched_card = item['card_name']
                                                matched_info = item
                                                item['summary'] = extract_purchased_items(text)
                                                break
                                    
                                    writer = PdfWriter()
                                    writer.add_page(reader.pages[page_num])
                                    
                                    if matched_card and matched_info:
                                        safe_date = sanitize_filename(matched_info['date'])
                                        safe_shop = sanitize_filename(matched_info['shop'])
                                        new_filename = f"{safe_date}_{safe_shop}_{matched_info['amount_comma']}円.pdf"
                                        target_dir = os.path.join(output_dir, matched_card)
                                    else:
                                        base_name = os.path.splitext(pdf_file.name)[0]
                                        new_filename = f"{base_name}_P{page_num + 1}.pdf"
                                        
                                        if any(k in text_norm for k in PAYPAY_KEYWORDS):
                                            target_dir = os.path.join(output_dir, '02_PayPay支払い分')
                                        elif any(k in text_norm for k in CARD_KEYWORDS):
                                            target_dir = os.path.join(output_dir, '03_未照合カード')
                                            # 未照合カードのエクセル（CSV）用リストに追加
                                            snippet = text.replace('\n', ' ')[:50] # 最初の50文字をプレビューとして取得
                                            unmatched_list.append([new_filename, snippet])
                                        elif any(k in text_norm for k in CASH_KEYWORDS):
                                            target_dir = os.path.join(output_dir, '04_現金支払い分')
                                        else:
                                            target_dir = os.path.join(output_dir, '05_その他（手動確認）')
                                    
                                    if not os.path.exists(target_dir): os.makedirs(target_dir)
                                    out_path = os.path.join(target_dir, new_filename)
                                    
                                    counter = 1
                                    while os.path.exists(out_path):
                                        new_filename = f"{os.path.splitext(new_filename)[0]}_{counter}.pdf"
                                        out_path = os.path.join(target_dir, new_filename)
                                        counter += 1
                                        
                                    with open(out_path, "wb") as f_out:
                                        writer.write(f_out)
                        except Exception as e:
                            st.error(f"PDF処理エラー ({pdf_file.name}): {e}")

                    # 全体照合レポートの作成
                    report_data = [["カード明細", "利用日", "店舗名", "金額", "状況", "AI抽出（買ったもの）"]]
                    missing = 0
                    for item in statements:
                        status = "〇 提出済" if item['matched'] else "× 未提出"
                        if not item['matched']: missing += 1
                        report_data.append([item['card_name'], item['date'], item['shop'], item['amount_comma'], status, item['summary']])
                    
                    if statements:
                        report_path = os.path.join(output_dir, "📝全体_レシート照合結果レポート.csv")
                        with open(report_path, 'w', encoding='utf-8-sig', newline='') as f:
                            writer = csv.writer(f)
                            writer.writerows(report_data)

                    # 未照合カード一覧の作成（追加機能）
                    if unmatched_list:
                        unmatched_path = os.path.join(output_dir, '03_未照合カード', "📝未照合カード一覧.csv")
                        if not os.path.exists(os.path.join(output_dir, '03_未照合カード')):
                            os.makedirs(os.path.join(output_dir, '03_未照合カード'))
                        with open(unmatched_path, 'w', encoding='utf-8-sig', newline='') as f:
                            writer = csv.writer(f)
                            writer.writerow(["ファイル名", "レシート内テキスト（一部抜粋）"])
                            writer.writerows(unmatched_list)

                    # ZIPファイル化
                    zip_path = os.path.join(temp_dir, "仕分け結果.zip")
                    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                        for root, _, files in os.walk(output_dir):
                            for file in files:
                                file_path = os.path.join(root, file)
                                arcname = os.path.relpath(file_path, output_dir)
                                zipf.write(file_path, arcname)

                    st.success(f"🐰 処理が完了しました。 (未提出レシート: {missing}件)")
                    
                    with open(zip_path, "rb") as f:
                        st.download_button(
                            label="📥 整理されたフォルダ（ZIP）をダウンロード",
                            data=f,
                            file_name="仕分け結果.zip",
                            mime="application/zip",
                            type="primary"
                        )

# ==========================================
# タブ2：明細PDF → CSV変換機能
# ==========================================
with tab2:
    st.subheader("🐱 スキャンした明細PDFをCSVに変換します")
    st.info("⚠️ **ご注意:** カード会社によってレイアウトが異なるため、プログラムによる読み取りは100%完全ではありません。変換後のCSVは必ず目視で確認・修正を行ってください。確実なデータが必要な場合は、カード会社の会員サイトからのCSVダウンロードを強く推奨いたします。")
    
    statement_pdf = st.file_uploader("📑 明細PDFをアップロード", type="pdf")
    
    if st.button("🐾 CSVに変換する", type="secondary"):
        if not statement_pdf:
            st.warning("⚠️ 明細PDFがアップロードされていません。")
        else:
            with st.spinner('🐱 テキストを抽出しています...'):
                extracted_lines = []
                try:
                    pdf_bytes = io.BytesIO(statement_pdf.getvalue())
                    with pdfplumber.open(pdf_bytes) as pdf:
                        for page in pdf.pages:
                            # 簡易的にテキストを行ごとに分割してCSV化
                            text = page.extract_text()
                            if text:
                                for line in text.split('\n'):
                                    # スペースで分割して列とみなす（簡易処理）
                                    extracted_lines.append(line.split(' '))
                                    
                    if extracted_lines:
                        # メモリ上でCSVを作成
                        output = io.StringIO()
                        writer = csv.writer(output)
                        writer.writerows(extracted_lines)
                        csv_data = output.getvalue().encode('utf-8-sig') # エクセル対応
                        
                        st.success("🐱 抽出が完了しました！以下のボタンからダウンロードしてください。")
                        st.download_button(
                            label="📥 変換したCSVをダウンロード",
                            data=csv_data,
                            file_name=f"明細変換_{statement_pdf.name}.csv",
                            mime="text/csv",
                            type="primary"
                        )
                    else:
                        st.error("❌ 文字を読み取れませんでした。手書きや画質が粗い可能性があります。")
                except Exception as e:
                    st.error(f"エラーが発生しました: {e}")
