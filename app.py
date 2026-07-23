import streamlit as st
import os
import shutil
import csv
import pdfplumber
from pypdf import PdfReader, PdfWriter
import zipfile
import tempfile
import io

# 🎨 画面の基本設定（かわいく！）
st.set_page_config(page_title="レシート自動仕分けアプリ♡", page_icon="🎀", layout="centered")

st.title("🎀 事務所専用：レシート仕分けマジック")
st.markdown("明細CSVとレシートPDFを入れるだけで、AIが魔法のように自動整理します✨")
st.divider()

# キーワード設定
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

# 📂 ファイルアップロード画面
st.subheader("1️⃣ データをセットしてください")
col1, col2 = st.columns(2)
with col1:
    csv_files = st.file_uploader("💳 カード明細（CSV）", type="csv", accept_multiple_files=True)
with col2:
    pdf_files = st.file_uploader("🧾 レシート（PDF）", type="pdf", accept_multiple_files=True)

if st.button("✨ 仕分けマジックをスタート！ ✨", use_container_width=True, type="primary"):
    if not pdf_files:
        st.warning("⚠️ レシートPDFを入れてください！")
    else:
        with st.spinner('AIが一生懸命仕分けしています...♡'):
            # 一時フォルダを作成して処理
            with tempfile.TemporaryDirectory() as temp_dir:
                output_dir = os.path.join(temp_dir, "03_仕分け結果")
                os.makedirs(output_dir)
                
                # CSVの読み込み
                statements = []
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

                # レポート作成
                report_data = [["カード明細", "利用日", "店舗名", "金額", "状況", "AI抽出（買ったもの）"]]
                missing = 0
                for item in statements:
                    status = "〇 提出済" if item['matched'] else "× 未提出"
                    if not item['matched']: missing += 1
                    report_data.append([item['card_name'], item['date'], item['shop'], item['amount_comma'], status, item['summary']])
                
                if statements:
                    report_path = os.path.join(output_dir, "✨レシート照合結果レポート.csv")
                    with open(report_path, 'w', encoding='utf-8-sig', newline='') as f:
                        writer = csv.writer(f)
                        writer.writerows(report_data)

                # ZIPファイルにまとめる
                zip_path = os.path.join(temp_dir, "仕分け結果.zip")
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for root, _, files in os.walk(output_dir):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, output_dir)
                            zipf.write(file_path, arcname)

                # 完了表示とダウンロードボタン
                st.success(f"🎉 仕分け完了！ (未提出レシート: {missing}件)")
                st.balloons()
                
                with open(zip_path, "rb") as f:
                    st.download_button(
                        label="📥 キレイに整理されたフォルダ（ZIP）をダウンロード！",
                        data=f,
                        file_name="仕分け結果.zip",
                        mime="application/zip",
                        type="primary"
                    )
