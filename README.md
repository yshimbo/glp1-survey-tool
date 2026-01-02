# GLP-1 Diet Trends Survey Tool

GLP-1関連のダイエット・医薬品トレンドを自動収集・分析するツール

## 機能

- FDA承認/申請情報の自動取得
- Drug Shortage監視（0件も表示）
- 複数ニュースソースからの記事収集
- 前回実行との差分表示
- 適応症・薬品名での検索

## 使用方法

### Google Colabで実行

```python
# ライブラリインストール
!pip install requests beautifulsoup4 feedparser -q

# サーベイ実行
%run glp1_survey.py
results = run_survey()
```
### コマンド

```python
# 基本実行
results = run_survey()

# Markdown形式で出力
results = run_survey(output_format='markdown')

# 特定薬剤の検索
search_specific_drug('semaglutide')

# Drug Shortage確認
check_drug_shortage('liraglutide')

# 前回の差分情報を表示
show_last_diff()
```

## ファイル構成
glp1_config.json - 設定ファイル（検索キーワード、ソースURL等）
glp1_survey.py - メインスクリプト

## 情報ソース
FDA (Press Announcements, Drug Safety, MedWatch, Warning Letters, Drug Shortages, Novel Drug Approvals)
WHO News
CMS Innovation Models
Novo Nordisk News
Eli Lilly News (RSS)
STAT News, BioPharma Dive, Fierce Biotech, Endpoints News
NPR Health, Food Navigator USA

## バージョン
v4.3 - 差分機能付き

## ライセンス
MIT License 
