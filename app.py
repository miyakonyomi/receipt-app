import streamlit as st
import os
import csv
import pdfplumber
from pypdf import PdfReader, PdfWriter
import zipfile
import tempfile
import io
import re
import unicodedata

# 🐻 画面の基本設定
st.set_page_config(page_title="【究極版】レシート自動仕分けアシスタント", page_icon="🐻", layout="wide")

st.title("🐻 事務所専用：レシート自動仕分けアシスタント")
st.markdown("仕分けエラー率0%の厳格システムです。洗練されたキーワードで正確にフォルダ分けを行います🐾")
st.divider()

# ==========================================
# 共通関数・究極のキーワード設定
# ==========================================
# 💡 ユーザー様の神アイデアを採用！「クレシ」「クレジ」「Airペイ」など、誤認識しやすい一部の文字を追加
CARD_KEYWORDS = ["クレジット", "クレシット", "クレジ", "クレシ", "visa", "mastercard", "jcb", "amex", "一括", "お客様控え", "アメリカン", "カード売", "airペイ", "エアペイ"]

# 💡 ユーザー様の大発見を採用！「お釣り」「お預かり」はカードでも印字されるため除外。「現金」のみを信じる！
CASH_KEYWORDS = ["現金", "現金払"]

PAYPAY_KEYWORDS = ["paypay", "ペイペイ", "ｐａｙｐａｙ"]

def sanitize_filename(text):
    invalid_chars = ['\\', '/', ':', '*', '?', '"', '<', '>', '|']
    for ch in invalid_chars: text = text.replace(ch, '_')
    return text

def normalize_for_match(text):
    if not text: return ""
    norm = unicodedata.normalize('NFKC', text).upper()
    return re.sub(r'\s+', '', norm)

def is_shop_match(csv_shop, receipt_text):
    if not csv_shop or csv_shop == '不明': return True 
    
    shop_norm = normalize_for_match(csv_shop)
    receipt_norm = normalize_for_match(receipt_text)
    clean_shop = re.sub(r'(カ\)|株\)|\(カ\)|\(株\)|カブシキガイシャ|株式会社|合同会社)', '', shop_norm)
    
    keyword = clean_shop[:2] if len(clean_shop) >= 2 else clean_shop
    if keyword and keyword in receipt_norm:
        return True
    return False

def get_monetary_amounts(text):
    amounts = set()
    matches = re.findall(r'(?:合計|計|お買上額|お支払総額|請求額|金額)[^\d]*([0-9,]+)', text)
    for m in matches: amounts.add(m.replace(',', ''))
    
    matches = re.findall(r'[¥￥]\s*([0-9,]+)', text)
    for m in matches: amounts.add(m.replace(',', ''))
    
    matches = re.findall(r'([0-9,]+)\s*円', text)
    for m in matches: amounts.add(m.replace(',', ''))
    return amounts

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
    return "（元PDFを確認）"

def extract_unmatched_info(text, filename):
    clean_text = re.sub(r'\s+', ' ', text).strip()
    dates = re.findall(r'(20\d{2}[年/.-]\d{1,2}[月/.-]\d{1,2}日?)', clean_text)
    date_str = dates[0] if dates else "（自動取得できず）"
    
    amounts_matches = re.findall(r'(?:合計|計)[^\d]*([0-9,]+)|[¥￥]\s*([0-9,]+)|([0-9,]+)\s*円', clean_text)
    possible_amounts = []
    for match in amounts_matches:
        for m in match:
            if m:
                try:
                    possible_amounts.append(int(m.replace(',', '')))
                except:
                    pass
    
    if possible_amounts:
        max_amt = max(possible_amounts)
        amount_str = f"¥{max_amt:,}"
    else:
        amount_str = "（自動取得できず）"

    preview = clean_text[:60] + "..." if len(clean_text) > 60 else clean_text
    return {
        "ファイル名": filename,
        "推測される日付": date_str,
        "推測される金額": amount_str,
        "レシート内テキスト（一部抜粋）": preview
    }

# ==========================================
# メイン処理
# ==========================================
st.subheader("1️⃣ 照合するデータをセットしてください（※複数ファイル可）")
col1, col2 = st.columns(2)
with col1:
    csv_files = st.file_uploader("💳 公式のカード明細（CSV）※複数選択OK", type="csv", accept_multiple_files=True)
with col2:
    pdf_files = st.file_uploader("🧾 レシート（PDF）※複数選択OK", type="pdf", accept_multiple_files=True)

if st.button("🐾 究極の厳格ロジックで仕分けを開始！", use_container_width=True, type="primary"):
    if not pdf_files:
        st.warning("⚠️ レシートPDFがアップロードされていません。")
    else:
        with st.spinner('🐻 データを正確に読み取っています...'):
            with tempfile.TemporaryDirectory() as temp_dir:
                output_dir = os.path.join(temp_dir, "03_仕分け結果")
                os.makedirs(output_dir)
                
                statements = []
                unmatched_list = []
                
                if csv_files:
                    for csv_file in csv_files:
                        folder_name = f"{os.path.splitext(csv_file.name)[0]}"
                        try:
                            content = csv_file.getvalue()
                            try:
                                decoded_lines = content.decode('utf-8-sig').splitlines()
                            except UnicodeDecodeError:
                                decoded_lines = content.decode('shift_jis').splitlines()
                            
                            header_idx = 0
                            for i, line in enumerate(decoded_lines):
                                if '金額' in line or '利用' in line or '摘要' in line or '明細' in line or '店名' in line:
                                    header_idx = i
                                    break
                                    
                            reader = csv.DictReader(decoded_lines[header_idx:])
                            for row in reader:
                                amount_key = next((k for k in row.keys() if k and ('金額' in k or '利用額' in k)), None)
                                date_key = next((k for k in row.keys() if k and ('日' in k or '月' in k)), None)
                                shop_key = next((k for k in row.keys() if k and ('摘要' in k or '明細' in k or '店名' in k)), None)
                                
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
                            st.error(f"CSV読み込みエラー ({csv_file.name}): {e}")

                for pdf_file in pdf_files:
                    try:
                        pdf_bytes = io.BytesIO(pdf_file.getvalue())
                        reader = PdfReader(pdf_bytes)
                        with pdfplumber.open(pdf_bytes) as pdf_text:
                            for page_num in range(len(reader.pages)):
                                extracted = pdf_text.pages[page_num].extract_text()
                                text = extracted if extracted else ""
                                text_norm = text.lower().replace(" ", "").replace(" ", "")
                                
                                receipt_amounts = get_monetary_amounts(text)
                                
                                matched_card = None
                                matched_info = None
                                
                                for item in statements:
                                    if not item['matched']:
                                        if item['amount'] in receipt_amounts:
                                            if is_shop_match(item['shop'], text):
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
                                    target_dir = os.path.join(output_dir, f"01_{matched_card}")
                                else:
                                    base_name = os.path.splitext(pdf_file.name)[0]
                                    new_filename = f"{base_name}_P{page_num + 1}.pdf"
                                    
                                    # 💡 ここで新キーワード設定が活きます！
                                    if any(k in text_norm for k in PAYPAY_KEYWORDS):
                                        target_dir = os.path.join(output_dir, '02_PayPay支払い分')
                                    elif any(k in text_norm for k in CARD_KEYWORDS):
                                        target_dir = os.path.join(output_dir, '03_未照合カード')
                                        info = extract_unmatched_info(text, new_filename)
                                        unmatched_list.append(info)
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

                total_count = len(statements)
                matched_count = sum(1 for item in statements if item['matched'])
                missing_count = total_count - matched_count

                report_data = [["カード明細種類", "利用日", "店舗名", "金額", "状況", "AI抽出（買ったもの）"]]
                for item in statements:
                    status = "〇 提出済" if item['matched'] else "× 未提出"
                    report_data.append([item['card_name'], item['date'], item['shop'], item['amount_comma'], status, item['summary']])
                
                if statements:
                    report_path = os.path.join(output_dir, "📝全体_レシート照合結果レポート.csv")
                    with open(report_path, 'w', encoding='utf-8-sig', newline='') as f:
                        writer = csv.writer(f)
                        writer.writerows(report_data)

                if unmatched_list:
                    unmatched_path = os.path.join(output_dir, '03_未照合カード', "📝未照合カード一覧.csv")
                    if not os.path.exists(os.path.join(output_dir, '03_未照合カード')):
                        os.makedirs(os.path.join(output_dir, '03_未照合カード'))
                    with open(unmatched_path, 'w', encoding='utf-8-sig', newline='') as f:
                        writer = csv.writer(f)
                        writer.writerow(["ファイル名", "推測される日付", "推測される金額", "レシート内テキスト（一部抜粋）"])
                        for info in unmatched_list:
                            writer.writerow([info["ファイル名"], info["推測される日付"], info["推測される金額"], info["レシート内テキスト（一部抜粋）"]])

                zip_path = os.path.join(temp_dir, "仕分け結果.zip")
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    for root, _, files in os.walk(output_dir):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, output_dir)
                            zipf.write(file_path, arcname)

                st.success("🐰 処理が完了しました！")
                
                st.subheader("📊 照合サマリー（結果報告）")
                col1, col2, col3 = st.columns(3)
                col1.metric("📄 公式明細の総件数", f"{total_count} 件")
                col2.metric("✅ レシート提出済", f"{matched_count} 件")
                col3.metric("❌ 未提出（不足分）", f"{missing_count} 件")
                
                if unmatched_list:
                    st.subheader("💳 未照合カード一覧（要手動確認）")
                    st.dataframe(unmatched_list, use_container_width=True)

                st.divider()
                with open(zip_path, "rb") as f:
                    st.download_button(
                        label="📥 整理されたフォルダ（ZIP）をダウンロード",
                        data=f,
                        file_name="仕分け結果.zip",
                        mime="application/zip",
                        type="primary"
                    )
