#!/usr/bin/env python3
"""
GLP-1 Diet Trends Survey Script (Google Colab版) v4.3
======================================================
設定ファイル分離版 - 差分機能付き
- 前回実行との差分表示
- 新規記事のハイライト
- Drug Shortageステータス変化の検出
- 404エラーのグレースフル処理
- 剤形（dosage_form）表示対応
- FDA申請状況（承認/申請）追跡対応
- Drug Shortage 0件表示対応
- 年度自動対応（Novel Drug Approvals）
"""

import json
import hashlib
import re
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Set, Tuple
from pathlib import Path
from urllib.parse import urljoin, urlparse
import warnings
warnings.filterwarnings('ignore')

import requests
from bs4 import BeautifulSoup
import feedparser


# ============================================================
# 設定ファイル読み込み
# ============================================================

class ConfigLoader:
    """設定ファイルの読み込みと管理"""
    
    def __init__(self, config_path: str = "/content/glp1_config.json"):
        self.config_path = config_path
        self.config = self._load_config()
    
    def _load_config(self) -> Dict:
        """設定ファイルを読み込む"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"⚠️ 設定ファイルが見つかりません: {self.config_path}")
            print("デフォルト設定を使用します")
            return self._get_default_config()
        except json.JSONDecodeError as e:
            print(f"⚠️ 設定ファイルの解析エラー: {e}")
            return self._get_default_config()
    
    def _get_default_config(self) -> Dict:
        """デフォルト設定を返す"""
        return {
            "general": {
                "output_dir": "/content/survey_output",
                "seen_urls_file": "seen_urls.json",
                "snapshot_file": "last_snapshot.json",
                "request_timeout": 30,
                "request_delay": 1.0,
                "max_articles_per_source": 50
            },
            "search_terms": {"indications": {}, "drug_names": {}, "drug_classes": {}},
            "sources": [],
            "shortage_monitor": {"enabled": False, "drugs_to_monitor": []}
        }
    
    def get(self, *keys, default=None):
        """ネストされたキーから値を取得"""
        value = self.config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value
    
    def reload(self):
        """設定ファイルを再読み込み"""
        self.config = self._load_config()
        print("✅ 設定ファイルを再読み込みしました")


# ============================================================
# データモデル
# ============================================================

@dataclass
class Article:
    """記事データモデル"""
    title: str
    url: str
    source: str
    category: str
    published_date: Optional[str] = None
    summary: Optional[str] = None
    content: Optional[str] = None
    collected_at: str = field(default_factory=lambda: datetime.now().isoformat())
    subcategory: Optional[str] = None
    relevance_score: float = 0.0
    matched_terms: List[str] = field(default_factory=list)
    dosage_form: Optional[str] = None
    submission_status: Optional[str] = None
    is_new: bool = False  # 差分表示用
    
    @property
    def hash_id(self) -> str:
        return hashlib.md5(self.url.encode()).hexdigest()[:12]


@dataclass
class DrugApproval:
    """FDA承認/申請情報データモデル"""
    drug_name: str
    brand_name: Optional[str] = None
    sponsor: Optional[str] = None
    application_number: Optional[str] = None
    approval_date: Optional[str] = None
    submission_date: Optional[str] = None
    submission_status: str = "unknown"
    indication: Optional[str] = None
    dosage_form: Optional[str] = None
    route: Optional[str] = None
    strength: Optional[str] = None
    url: Optional[str] = None
    is_new: bool = False  # 差分表示用
    
    def to_article(self, source: str = "FDA") -> Article:
        """Article形式に変換"""
        status_emoji = self._get_status_emoji()
        dosage_info = f" [{self.dosage_form}]" if self.dosage_form else ""
        
        title = f"{status_emoji} {self.brand_name or self.drug_name}{dosage_info}"
        if self.indication:
            title += f" - {self.indication[:50]}"
        
        summary_parts = []
        if self.submission_status:
            summary_parts.append(f"ステータス: {self.submission_status}")
        if self.approval_date:
            summary_parts.append(f"承認日: {self.approval_date}")
        if self.submission_date:
            summary_parts.append(f"申請日: {self.submission_date}")
        if self.sponsor:
            summary_parts.append(f"申請者: {self.sponsor}")
        if self.dosage_form:
            summary_parts.append(f"剤形: {self.dosage_form}")
        if self.route:
            summary_parts.append(f"投与経路: {self.route}")
        if self.application_number:
            summary_parts.append(f"申請番号: {self.application_number}")
        
        return Article(
            title=title,
            url=self.url or "",
            source=source,
            category="government",
            subcategory="fda_approval",
            published_date=self.approval_date or self.submission_date,
            summary=" | ".join(summary_parts),
            dosage_form=self.dosage_form,
            submission_status=self.submission_status,
            is_new=self.is_new
        )
    
    def _get_status_emoji(self) -> str:
        """ステータスに対応する絵文字を取得"""
        status_map = {
            "approved": "✅", "承認": "✅",
            "tentative": "☑️", "暫定": "☑️",
            "filed": "📄", "受理": "📄",
            "submitted": "📝", "申請": "📝",
            "review": "🔍", "審査": "🔍",
            "pending": "⏳",
            "not_approved": "❌", "非承認": "❌",
            "withdrawn": "🔙", "撤回": "🔙"
        }
        status_lower = self.submission_status.lower() if self.submission_status else ""
        for key, emoji in status_map.items():
            if key in status_lower:
                return emoji
        return "📋"


@dataclass
class SourceConfig:
    """情報ソース設定"""
    name: str
    url: str
    category: str
    source_type: str
    enabled: bool = True
    priority: int = 2
    rss_url: Optional[str] = None
    subcategory: Optional[str] = None
    selectors: Optional[Dict[str, str]] = None
    dynamic_year: bool = False


@dataclass
class SurveySnapshot:
    """サーベイのスナップショット（差分比較用）"""
    timestamp: str
    article_urls: Set[str]
    shortage_status: Dict[str, str]
    fda_approval_ids: Set[str]
    article_count: int
    fda_count: int
    shortage_count: int


# ============================================================
# 差分管理クラス
# ============================================================

class DiffManager:
    """前回実行との差分を管理するクラス"""
    
    def __init__(self, output_dir: Path, config: ConfigLoader):
        self.output_dir = output_dir
        self.config = config
        self.snapshot_file = output_dir / config.get("general", "snapshot_file", default="last_snapshot.json")
        self.previous_snapshot: Optional[SurveySnapshot] = None
        self.current_snapshot: Optional[SurveySnapshot] = None
        self._load_previous_snapshot()
    
    def _load_previous_snapshot(self):
        """前回のスナップショットを読み込む"""
        try:
            if self.snapshot_file.exists():
                with open(self.snapshot_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.previous_snapshot = SurveySnapshot(
                        timestamp=data.get('timestamp', ''),
                        article_urls=set(data.get('article_urls', [])),
                        shortage_status=data.get('shortage_status', {}),
                        fda_approval_ids=set(data.get('fda_approval_ids', [])),
                        article_count=data.get('article_count', 0),
                        fda_count=data.get('fda_count', 0),
                        shortage_count=data.get('shortage_count', 0)
                    )
                    try:
                        prev_dt = datetime.fromisoformat(self.previous_snapshot.timestamp)
                        prev_time_str = prev_dt.strftime('%Y-%m-%d %H:%M')
                    except:
                        prev_time_str = self.previous_snapshot.timestamp
                    print(f"  📂 前回のスナップショットを読み込みました ({prev_time_str})")
        except Exception as e:
            print(f"  ℹ️ 前回のスナップショットがありません（初回実行）")
            self.previous_snapshot = None
    
    def create_current_snapshot(self, articles: List[Article], 
                                 shortage_articles: List[Article],
                                 fda_approvals: List[DrugApproval]) -> SurveySnapshot:
        """現在のサーベイ結果からスナップショットを作成"""
        shortage_status = {}
        for article in shortage_articles:
            if "⚠️ 供給不足:" in article.title:
                drug_name = article.title.replace("⚠️ 供給不足:", "").strip()
                shortage_status[drug_name] = "shortage"
            elif "✅ 供給正常:" in article.title or "✅ 供給解消:" in article.title:
                drug_name = article.title.replace("✅ 供給正常:", "").replace("✅ 供給解消:", "").strip()
                drug_name = drug_name.replace("(リスト記載なし)", "").strip()
                shortage_status[drug_name] = "normal"
        
        self.current_snapshot = SurveySnapshot(
            timestamp=datetime.now().isoformat(),
            article_urls=set(a.url for a in articles),
            shortage_status=shortage_status,
            fda_approval_ids=set(a.application_number or f"{a.drug_name}_{a.brand_name}" for a in fda_approvals),
            article_count=len(articles),
            fda_count=len(fda_approvals),
            shortage_count=len(shortage_articles)
        )
        return self.current_snapshot
    
    def save_current_snapshot(self):
        """現在のスナップショットを保存"""
        if not self.current_snapshot:
            return
        
        try:
            data = {
                'timestamp': self.current_snapshot.timestamp,
                'article_urls': list(self.current_snapshot.article_urls),
                'shortage_status': self.current_snapshot.shortage_status,
                'fda_approval_ids': list(self.current_snapshot.fda_approval_ids),
                'article_count': self.current_snapshot.article_count,
                'fda_count': self.current_snapshot.fda_count,
                'shortage_count': self.current_snapshot.shortage_count
            }
            with open(self.snapshot_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"  💾 スナップショットを保存しました")
        except Exception as e:
            print(f"  ⚠️ スナップショット保存エラー: {e}")
    
    def get_diff(self, articles: List[Article], 
                 shortage_articles: List[Article],
                 fda_approvals: List[DrugApproval]) -> Dict[str, Any]:
        """前回との差分を計算"""
        diff = {
            'has_previous': self.previous_snapshot is not None,
            'previous_timestamp': None,
            'previous_timestamp_display': None,
            'new_articles': [],
            'new_article_count': 0,
            'removed_article_count': 0,
            'shortage_changes': [],
            'new_fda_approvals': [],
            'summary': {
                'articles_delta': 0,
                'fda_delta': 0,
                'has_shortage_changes': False
            }
        }
        
        if not self.previous_snapshot:
            diff['new_articles'] = articles
            diff['new_article_count'] = len(articles)
            diff['new_fda_approvals'] = fda_approvals
            for a in articles:
                a.is_new = True
            for a in fda_approvals:
                a.is_new = True
            return diff
        
        diff['previous_timestamp'] = self.previous_snapshot.timestamp
        try:
            prev_dt = datetime.fromisoformat(self.previous_snapshot.timestamp)
            diff['previous_timestamp_display'] = prev_dt.strftime('%Y-%m-%d %H:%M')
        except:
            diff['previous_timestamp_display'] = self.previous_snapshot.timestamp
        
        # 新規記事の検出
        prev_urls = self.previous_snapshot.article_urls
        for article in articles:
            if article.url not in prev_urls:
                article.is_new = True
                diff['new_articles'].append(article)
        diff['new_article_count'] = len(diff['new_articles'])
        
        # 削除された記事数
        current_urls = set(a.url for a in articles)
        diff['removed_article_count'] = len(prev_urls - current_urls)
        
        # Drug Shortage状態の変化を検出
        prev_shortage = self.previous_snapshot.shortage_status
        current_shortage = {}
        for article in shortage_articles:
            if "⚠️ 供給不足:" in article.title:
                drug_name = article.title.replace("⚠️ 供給不足:", "").strip()
                current_shortage[drug_name] = "shortage"
            elif "✅ 供給正常:" in article.title or "✅ 供給解消:" in article.title:
                drug_name = article.title.replace("✅ 供給正常:", "").replace("✅ 供給解消:", "").strip()
                drug_name = drug_name.replace("(リスト記載なし)", "").strip()
                current_shortage[drug_name] = "normal"
        
        all_drugs = set(prev_shortage.keys()) | set(current_shortage.keys())
        for drug in all_drugs:
            prev_status = prev_shortage.get(drug, "unknown")
            curr_status = current_shortage.get(drug, "unknown")
            
            if prev_status != curr_status:
                if prev_status == "normal" and curr_status == "shortage":
                    diff['shortage_changes'].append({
                        'drug': drug,
                        'change': 'new_shortage',
                        'message': f"🔴 新規供給不足: {drug}",
                        'severity': 'high'
                    })
                elif prev_status == "shortage" and curr_status == "normal":
                    diff['shortage_changes'].append({
                        'drug': drug,
                        'change': 'resolved',
                        'message': f"🟢 供給不足解消: {drug}",
                        'severity': 'info'
                    })
                elif prev_status == "unknown" and curr_status == "shortage":
                    diff['shortage_changes'].append({
                        'drug': drug,
                        'change': 'new_shortage',
                        'message': f"🔴 供給不足検出: {drug}",
                        'severity': 'high'
                    })
        
        # 新規FDA承認の検出
        prev_fda_ids = self.previous_snapshot.fda_approval_ids
        for approval in fda_approvals:
            approval_id = approval.application_number or f"{approval.drug_name}_{approval.brand_name}"
            if approval_id not in prev_fda_ids:
                approval.is_new = True
                diff['new_fda_approvals'].append(approval)
        
        # サマリー
        diff['summary']['articles_delta'] = len(articles) - self.previous_snapshot.article_count
        diff['summary']['fda_delta'] = len(fda_approvals) - self.previous_snapshot.fda_count
        diff['summary']['has_shortage_changes'] = len(diff['shortage_changes']) > 0
        
        return diff


# ============================================================
# 関連性判定
# ============================================================

class RelevanceMatcher:
    """GLP-1関連キーワードによる関連性判定"""
    
    def __init__(self, config: ConfigLoader):
        self.config = config
        self.weights = config.get("relevance_weights", default={
            "indication": 3, "drug_class": 3, "drug_name": 4,
            "brand_name": 4, "company": 1, "regulatory": 2
        })
        self._build_patterns()
    
    def _build_patterns(self):
        """検索パターンを構築"""
        self.patterns = {
            "indication": [],
            "drug_class": [],
            "drug_name": [],
            "brand_name": [],
            "company": [],
            "regulatory": []
        }
        
        indications = self.config.get("search_terms", "indications", default={})
        for ind_data in indications.values():
            for alias in ind_data.get("aliases", []):
                self.patterns["indication"].append(alias.lower())
        
        drug_classes = self.config.get("search_terms", "drug_classes", default={})
        for class_data in drug_classes.values():
            for alias in class_data.get("aliases", []):
                self.patterns["drug_class"].append(alias.lower())
        
        drug_names = self.config.get("search_terms", "drug_names", default={})
        for drug_data in drug_names.values():
            for alias in drug_data.get("aliases", []):
                self.patterns["drug_name"].append(alias.lower())
            for brand in drug_data.get("brands", []):
                self.patterns["brand_name"].append(brand.lower())
        
        companies = self.config.get("search_terms", "companies", default={})
        for company_data in companies.values():
            for alias in company_data.get("aliases", []):
                self.patterns["company"].append(alias.lower())
        
        regulatory = self.config.get("search_terms", "regulatory_terms", default={})
        for alias in regulatory.get("aliases", []):
            self.patterns["regulatory"].append(alias.lower())
    
    def calculate_relevance(self, text: str) -> Tuple[float, List[str]]:
        """テキストの関連性スコアと一致キーワードを計算"""
        if not text:
            return 0.0, []
        
        text_lower = text.lower()
        total_score = 0.0
        matched_terms = []
        
        for category, patterns in self.patterns.items():
            weight = self.weights.get(category, 1)
            for pattern in patterns:
                if pattern in text_lower:
                    total_score += weight
                    if pattern not in matched_terms:
                        matched_terms.append(pattern)
        
        return total_score, matched_terms
    
    def is_relevant(self, text: str, threshold: float = 1.0) -> bool:
        """テキストが関連性閾値を超えるか判定"""
        score, _ = self.calculate_relevance(text)
        return score >= threshold


# ============================================================
# スクレイパー基底クラス
# ============================================================

class BaseScraper:
    """スクレイパー基底クラス"""
    
    def __init__(self, config: ConfigLoader, matcher: RelevanceMatcher):
        self.config = config
        self.matcher = matcher
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': config.get("general", "user_agent", 
                default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        })
        self.timeout = config.get("general", "request_timeout", default=30)
        self.delay = config.get("general", "request_delay", default=1.0)
    
    def fetch(self, url: str) -> Optional[str]:
        """URLからコンテンツを取得"""
        try:
            time.sleep(self.delay)
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            return response.text
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else "Unknown"
            if status_code == 404:
                print(f" (404)", end="")
            elif status_code == 403:
                print(f" (403)", end="")
            else:
                print(f" (HTTP {status_code})", end="")
            return None
        except requests.exceptions.Timeout:
            print(f" (timeout)", end="")
            return None
        except requests.exceptions.RequestException as e:
            print(f" (error)", end="")
            return None
    
    def fetch_rss(self, url: str) -> Optional[feedparser.FeedParserDict]:
        """RSSフィードを取得"""
        try:
            time.sleep(self.delay)
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                print(f" (RSS error)", end="")
                return None
            return feed
        except Exception as e:
            print(f" (RSS error)", end="")
            return None


# ============================================================
# RSSフェッチャー
# ============================================================

class RSSFetcher(BaseScraper):
    """RSSフィード取得クラス"""
    
    def fetch_articles(self, source: SourceConfig) -> List[Article]:
        """RSSから記事を取得"""
        articles = []
        rss_url = source.rss_url or source.url
        
        feed = self.fetch_rss(rss_url)
        if not feed:
            return articles
        
        max_articles = self.config.get("general", "max_articles_per_source", default=50)
        
        for entry in feed.entries[:max_articles]:
            title = entry.get('title', '')
            link = entry.get('link', '')
            summary = entry.get('summary', entry.get('description', ''))
            
            if summary:
                summary = BeautifulSoup(summary, 'html.parser').get_text()[:500]
            
            check_text = f"{title} {summary}"
            score, matched = self.matcher.calculate_relevance(check_text)
            
            if score > 0:
                pub_date = None
                if 'published_parsed' in entry and entry.published_parsed:
                    try:
                        pub_date = datetime(*entry.published_parsed[:6]).strftime('%Y-%m-%d')
                    except:
                        pass
                
                article = Article(
                    title=title,
                    url=link,
                    source=source.name,
                    category=source.category,
                    subcategory=source.subcategory,
                    published_date=pub_date,
                    summary=summary,
                    relevance_score=score,
                    matched_terms=matched
                )
                articles.append(article)
        
        return articles


# ============================================================
# Webスクレイパー
# ============================================================

class WebScraper(BaseScraper):
    """Webページスクレイピングクラス"""
    
    def fetch_articles(self, source: SourceConfig) -> List[Article]:
        """Webページから記事を取得"""
        articles = []
        html = self.fetch(source.url)
        
        if not html:
            return articles
        
        soup = BeautifulSoup(html, 'html.parser')
        selectors = source.selectors or {}
        
        article_selector = selectors.get('article', 'article, .news-item, .card')
        article_elements = soup.select(article_selector)[:self.config.get("general", "max_articles_per_source", default=50)]
        
        for elem in article_elements:
            try:
                title_selector = selectors.get('title', 'h2, h3, .title')
                title_elem = elem.select_one(title_selector)
                title = title_elem.get_text(strip=True) if title_elem else ""
                
                if not title:
                    continue
                
                link_selector = selectors.get('link', 'a')
                link_elem = elem.select_one(link_selector)
                link = ""
                if link_elem and link_elem.get('href'):
                    link = urljoin(source.url, link_elem['href'])
                
                score, matched = self.matcher.calculate_relevance(title)
                
                if score > 0:
                    date_selector = selectors.get('date', 'time, .date')
                    date_elem = elem.select_one(date_selector)
                    pub_date = date_elem.get_text(strip=True) if date_elem else None
                    
                    article = Article(
                        title=title,
                        url=link or source.url,
                        source=source.name,
                        category=source.category,
                        subcategory=source.subcategory,
                        published_date=pub_date,
                        relevance_score=score,
                        matched_terms=matched
                    )
                    articles.append(article)
            except Exception:
                continue
        
        return articles


# ============================================================
# FDA Novel Drug Approvals スクレイパー
# ============================================================

class NovelDrugApprovalsScraper(BaseScraper):
    """FDA Novel Drug Approvals ページスクレイパー（年度自動対応）"""
    
    BASE_URL_TEMPLATE = "https://www.fda.gov/drugs/novel-drug-approvals-fda/novel-drug-approvals-{year}"
    
    def fetch_articles(self, source: SourceConfig) -> List[Article]:
        """Novel Drug Approvalsページから記事を取得"""
        articles = []
        current_year = datetime.now().year
        years_to_check = [current_year, current_year - 1]
        
        for year in years_to_check:
            url = self.BASE_URL_TEMPLATE.format(year=year)
            print(f"\n    📅 {year}年...", end="", flush=True)
            
            html = self.fetch(url)
            if not html:
                continue
            
            soup = BeautifulSoup(html, 'html.parser')
            tables = soup.find_all('table')
            
            year_count = 0
            for table in tables:
                rows = table.find_all('tr')
                for row in rows[1:]:
                    cells = row.find_all('td')
                    if len(cells) >= 2:
                        try:
                            drug_cell = cells[0]
                            drug_name = drug_cell.get_text(strip=True)
                            
                            link_elem = drug_cell.find('a')
                            drug_url = ""
                            if link_elem and link_elem.get('href'):
                                drug_url = urljoin(url, link_elem['href'])
                            
                            full_text = " ".join([c.get_text(strip=True) for c in cells])
                            score, matched = self.matcher.calculate_relevance(full_text)
                            
                            if score > 0:
                                active_ingredient = cells[1].get_text(strip=True) if len(cells) > 1 else ""
                                approval_date = ""
                                for cell in cells:
                                    text = cell.get_text(strip=True)
                                    if re.match(r'\d{1,2}/\d{1,2}/\d{4}', text):
                                        approval_date = text
                                        break
                                
                                title = f"✅ FDA承認 ({year}): {drug_name}"
                                if active_ingredient and active_ingredient != drug_name:
                                    title += f" ({active_ingredient})"
                                
                                article = Article(
                                    title=title,
                                    url=drug_url or url,
                                    source=f"FDA Novel Drug Approvals {year}",
                                    category="government",
                                    subcategory="novel_approvals",
                                    published_date=approval_date,
                                    summary=f"有効成分: {active_ingredient}" if active_ingredient else None,
                                    relevance_score=score,
                                    matched_terms=matched
                                )
                                articles.append(article)
                                year_count += 1
                        except Exception:
                            continue
            
            print(f" {year_count}件", end="")
        
        return articles


# ============================================================
# FDA Warning Letters スクレイパー
# ============================================================

class WarningLettersScraper(BaseScraper):
    """FDA Warning Letters ページスクレイパー"""
    
    WARNING_LETTERS_URL = "https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/warning-letters"
    
    def fetch_articles(self, source: SourceConfig) -> List[Article]:
        """Warning Lettersページから記事を取得"""
        articles = []
        
        html = self.fetch(self.WARNING_LETTERS_URL)
        if not html:
            return articles
        
        soup = BeautifulSoup(html, 'html.parser')
        table = soup.find('table')
        if not table:
            return articles
        
        rows = table.find_all('tr')
        for row in rows[1:]:
            cells = row.find_all('td')
            if len(cells) >= 4:
                try:
                    posted_date = cells[0].get_text(strip=True)
                    issue_date = cells[1].get_text(strip=True)
                    
                    company_cell = cells[2]
                    company_link = company_cell.find('a')
                    company_name = company_link.get_text(strip=True) if company_link else company_cell.get_text(strip=True)
                    letter_url = ""
                    if company_link and company_link.get('href'):
                        letter_url = urljoin(self.WARNING_LETTERS_URL, company_link['href'])
                    
                    issuing_office = cells[3].get_text(strip=True) if len(cells) > 3 else ""
                    subject = cells[4].get_text(strip=True) if len(cells) > 4 else ""
                    
                    check_text = f"{company_name} {subject} {issuing_office}"
                    score, matched = self.matcher.calculate_relevance(check_text)
                    
                    if 'cder' in issuing_office.lower() or 'drug' in subject.lower():
                        score += 2
                    
                    if score > 0:
                        title = f"⚠️ FDA警告書: {company_name}"
                        if subject:
                            title += f" - {subject[:50]}"
                        
                        article = Article(
                            title=title,
                            url=letter_url or self.WARNING_LETTERS_URL,
                            source="FDA Warning Letters",
                            category="government",
                            subcategory="warning_letters",
                            published_date=posted_date,
                            summary=f"発行日: {issue_date} | 発行元: {issuing_office} | 内容: {subject}",
                            relevance_score=score,
                            matched_terms=matched
                        )
                        articles.append(article)
                except Exception:
                    continue
        
        return articles


# ============================================================
# Drug Shortage モニター
# ============================================================

class DrugShortageMonitor(BaseScraper):
    """FDA Drug Shortage Database 監視クラス"""
    
    SHORTAGE_DB_URL = "https://www.accessdata.fda.gov/scripts/drugshortages/default.cfm"
    
    def __init__(self, config: ConfigLoader, matcher: RelevanceMatcher):
        super().__init__(config, matcher)
        self._shortage_cache = None
        self._cache_time = None
    
    def _fetch_shortage_list(self) -> Dict[str, Dict]:
        """FDA Drug Shortage Database から不足リストを取得"""
        if self._shortage_cache and self._cache_time:
            if (datetime.now() - self._cache_time).seconds < 300:
                return self._shortage_cache
        
        shortage_data = {}
        
        try:
            html = self.fetch(self.SHORTAGE_DB_URL)
            if not html:
                print("\n    ⚠️ Drug Shortage Databaseにアクセスできませんでした")
                return shortage_data
            
            soup = BeautifulSoup(html, 'html.parser')
            table = soup.find('table')
            
            if table:
                rows = table.find_all('tr')
                for row in rows:
                    cells = row.find_all('td')
                    if len(cells) >= 2:
                        name_cell = cells[0]
                        status_cell = cells[1]
                        
                        link = name_cell.find('a')
                        if link:
                            drug_name = link.get_text(strip=True)
                            drug_url = link.get('href', '')
                            if drug_url and not drug_url.startswith('http'):
                                drug_url = f"https://www.accessdata.fda.gov/scripts/drugshortages/{drug_url}"
                            
                            status = status_cell.get_text(strip=True)
                            normalized_key = drug_name.lower().replace(' ', '').replace('-', '')
                            
                            shortage_data[normalized_key] = {
                                'name': drug_name,
                                'status': status,
                                'url': drug_url,
                                'in_shortage': 'currently in shortage' in status.lower()
                            }
            
            self._shortage_cache = shortage_data
            self._cache_time = datetime.now()
            print(f"\n    ✅ Drug Shortage Database: {len(shortage_data)}件取得")
            
        except Exception as e:
            print(f"\n    ⚠️ Drug Shortage Database取得エラー: {str(e)[:50]}")
        
        return shortage_data
    
    def check_shortages(self) -> List[Article]:
        """監視対象薬剤の不足状況をチェック"""
        articles = []
        
        shortage_config = self.config.get("shortage_monitor", default={})
        if not shortage_config.get("enabled", False):
            return articles
        
        show_zero = shortage_config.get("show_zero_results", True)
        drugs_to_monitor = shortage_config.get("drugs_to_monitor", [])
        brands_to_monitor = shortage_config.get("brands_to_monitor", [])
        
        print(f"    監視対象: {len(drugs_to_monitor) + len(brands_to_monitor)}件", end="")
        
        shortage_list = self._fetch_shortage_list()
        
        if not shortage_list:
            article = Article(
                title="⚠️ Drug Shortage Database アクセス不可",
                url=self.SHORTAGE_DB_URL,
                source="FDA Drug Shortages",
                category="government",
                subcategory="drug_shortages",
                summary="FDA Drug Shortage Databaseにアクセスできませんでした。",
                relevance_score=5.0
            )
            articles.append(article)
            return articles
        
        all_drugs_to_check = []
        for drug in drugs_to_monitor:
            all_drugs_to_check.append({'search_term': drug, 'type': 'generic', 'display_name': drug.title()})
        for brand in brands_to_monitor:
            all_drugs_to_check.append({'search_term': brand, 'type': 'brand', 'display_name': brand})
        
        shortage_count = 0
        normal_count = 0
        
        for drug_info in all_drugs_to_check:
            search_term = drug_info['search_term'].lower()
            display_name = drug_info['display_name']
            
            found_shortage = None
            for key, data in shortage_list.items():
                if search_term in key or search_term in data['name'].lower():
                    found_shortage = data
                    break
            
            if found_shortage and found_shortage['in_shortage']:
                shortage_count += 1
                article = Article(
                    title=f"⚠️ 供給不足: {found_shortage['name']}",
                    url=found_shortage['url'],
                    source="FDA Drug Shortages",
                    category="government",
                    subcategory="drug_shortages",
                    summary=f"ステータス: {found_shortage['status']}。処方・調剤時に供給状況を確認してください。",
                    relevance_score=10.0,
                    matched_terms=[display_name, "shortage", "supply"]
                )
                articles.append(article)
            elif found_shortage and not found_shortage['in_shortage']:
                normal_count += 1
                if show_zero:
                    article = Article(
                        title=f"✅ 供給解消: {found_shortage['name']}",
                        url=found_shortage['url'],
                        source="FDA Drug Shortages",
                        category="government",
                        subcategory="drug_shortages",
                        summary=f"ステータス: {found_shortage['status']}。以前の供給不足は解消されています。",
                        relevance_score=2.0,
                        matched_terms=[display_name, "resolved"]
                    )
                    articles.append(article)
            else:
                normal_count += 1
                if show_zero:
                    article = Article(
                        title=f"✅ 供給正常: {display_name} (リスト記載なし)",
                        url=self.SHORTAGE_DB_URL,
                        source="FDA Drug Shortages",
                        category="government",
                        subcategory="drug_shortages",
                        summary=f"{display_name}はFDA Drug Shortage Databaseに記載がありません。供給に問題はないと考えられます。",
                        relevance_score=1.0,
                        matched_terms=[display_name]
                    )
                    articles.append(article)
        
        print(f" (不足: {shortage_count}, 正常: {normal_count})")
        
        return articles


# ============================================================
# FDA APIクライアント
# ============================================================

class FDAApiClient(BaseScraper):
    """FDA API連携クライアント"""
    
    def __init__(self, config: ConfigLoader, matcher: RelevanceMatcher):
        super().__init__(config, matcher)
        self.base_url = config.get("fda_api", "base_url", default="https://api.fda.gov")
        self.endpoints = config.get("fda_api", "endpoints", default={
            "drugs_fda": "/drug/drugsfda.json",
            "drug_label": "/drug/label.json"
        })
    
    def search_by_drug_name(self, drug_name: str, limit: int = 10) -> List[DrugApproval]:
        """薬品名で検索"""
        endpoint = self.endpoints.get("drugs_fda", "/drug/drugsfda.json")
        url = f"{self.base_url}{endpoint}"
        
        params = {
            "search": f'openfda.generic_name:"{drug_name}" OR openfda.brand_name:"{drug_name}"',
            "limit": limit
        }
        
        return self._execute_search(url, params)
    
    def search_by_indication(self, indication: str, limit: int = 20) -> List[DrugApproval]:
        """適応症で検索"""
        endpoint = self.endpoints.get("drug_label", "/drug/label.json")
        url = f"{self.base_url}{endpoint}"
        
        params = {
            "search": f'indications_and_usage:"{indication}"',
            "limit": limit
        }
        
        return self._execute_search(url, params, is_label=True)
    
    def search_obesity_diabetes_drugs(self, limit: int = 50) -> List[DrugApproval]:
        """肥満・糖尿病関連薬を検索"""
        results = []
        
        indications = ["obesity", "weight loss", "type 2 diabetes"]
        for indication in indications:
            results.extend(self.search_by_indication(indication, limit=15))
        
        drug_names = self.config.get("search_terms", "drug_names", default={})
        for drug_key, drug_data in drug_names.items():
            for alias in drug_data.get("aliases", [])[:1]:
                results.extend(self.search_by_drug_name(alias, limit=5))
        
        seen = set()
        unique_results = []
        for r in results:
            key = f"{r.drug_name}_{r.application_number}"
            if key not in seen:
                seen.add(key)
                unique_results.append(r)
        
        return unique_results
    
    def _execute_search(self, url: str, params: Dict, is_label: bool = False) -> List[DrugApproval]:
        """検索を実行して結果をパース"""
        approvals = []
        
        try:
            time.sleep(self.delay)
            response = self.session.get(url, params=params, timeout=self.timeout)
            
            if response.status_code == 404:
                return approvals
            
            response.raise_for_status()
            data = response.json()
            
            results = data.get('results', [])
            for item in results:
                if is_label:
                    approval = self._parse_label_result(item)
                else:
                    approval = self._parse_drugsfda_result(item)
                
                if approval:
                    approvals.append(approval)
        
        except requests.exceptions.HTTPError:
            pass
        except json.JSONDecodeError:
            pass
        except Exception:
            pass
        
        return approvals
    
    def _parse_drugsfda_result(self, item: Dict) -> Optional[DrugApproval]:
        """Drugs@FDA結果をパース"""
        try:
            openfda = item.get('openfda', {})
            products = item.get('products', [{}])
            submissions = item.get('submissions', [{}])
            
            latest_submission = submissions[0] if submissions else {}
            product = products[0] if products else {}
            
            dosage_form = product.get('dosage_form', '')
            route = product.get('route', '')
            strength = product.get('active_ingredients', [{}])
            if strength and isinstance(strength[0], dict):
                strength = strength[0].get('strength', '')
            else:
                strength = ''
            
            status = latest_submission.get('submission_status', '')
            status_date = latest_submission.get('submission_status_date', '')
            
            return DrugApproval(
                drug_name=openfda.get('generic_name', [''])[0] if openfda.get('generic_name') else item.get('sponsor_name', ''),
                brand_name=openfda.get('brand_name', [''])[0] if openfda.get('brand_name') else None,
                sponsor=item.get('sponsor_name'),
                application_number=item.get('application_number'),
                approval_date=status_date if 'AP' in status else None,
                submission_date=status_date,
                submission_status=self._decode_status(status),
                dosage_form=dosage_form,
                route=route,
                strength=str(strength) if strength else None,
                url=f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={item.get('application_number', '')}"
            )
        except Exception:
            return None
    
    def _parse_label_result(self, item: Dict) -> Optional[DrugApproval]:
        """Drug Label結果をパース"""
        try:
            openfda = item.get('openfda', {})
            
            indications = item.get('indications_and_usage', [''])
            indication_text = indications[0][:200] if indications else ''
            
            dosage_form = openfda.get('dosage_form', [''])[0] if openfda.get('dosage_form') else ''
            route = openfda.get('route', [''])[0] if openfda.get('route') else ''
            
            return DrugApproval(
                drug_name=openfda.get('generic_name', [''])[0] if openfda.get('generic_name') else '',
                brand_name=openfda.get('brand_name', [''])[0] if openfda.get('brand_name') else None,
                sponsor=openfda.get('manufacturer_name', [''])[0] if openfda.get('manufacturer_name') else None,
                indication=indication_text,
                dosage_form=dosage_form,
                route=route,
                submission_status="approved"
            )
        except Exception:
            return None
    
    def _decode_status(self, status_code: str) -> str:
        """ステータスコードをデコード"""
        status_map = self.config.get("submission_status", default={})
        
        status_upper = status_code.upper()
        for status_key, status_data in status_map.items():
            codes = status_data.get("codes", [])
            for code in codes:
                if code in status_upper:
                    return status_data.get("display_name", status_code)
        
        default_map = {
            "AP": "承認済",
            "TA": "暫定承認",
            "NA": "非承認",
            "WD": "撤回"
        }
        return default_map.get(status_code, status_code)


# ============================================================
# レポート生成
# ============================================================

class ReportGenerator:
    """レポート生成クラス"""
    
    def __init__(self, config: ConfigLoader):
        self.config = config
    
    def generate(self, articles: List[Article], shortage_articles: List[Article],
                 fda_approvals: List[DrugApproval], format: str = "html",
                 diff: Dict[str, Any] = None) -> str:
        """レポートを生成"""
        if format == "markdown":
            return self._generate_markdown(articles, shortage_articles, fda_approvals, diff)
        elif format == "json":
            return self._generate_json(articles, shortage_articles, fda_approvals, diff)
        else:
            return self._generate_html(articles, shortage_articles, fda_approvals, diff)
    
    def _generate_html(self, articles: List[Article], shortage_articles: List[Article],
                       fda_approvals: List[DrugApproval], diff: Dict[str, Any] = None) -> str:
        """HTMLレポートを生成"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        
        # 差分セクションの生成
        diff_html = self._generate_diff_section_html(diff)
        
        html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GLP-1 Survey Report - {now}</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Hiragino Sans', sans-serif; margin: 0; padding: 20px; background: #f5f5f5; line-height: 1.6; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        h1 {{ color: #1a73e8; border-bottom: 3px solid #1a73e8; padding-bottom: 10px; }}
        h2 {{ color: #333; margin-top: 30px; padding: 10px 0; border-bottom: 2px solid #ddd; }}
        h3 {{ color: #555; margin-top: 20px; }}
        .summary {{ background: linear-gradient(135deg, #e8f4fd 0%, #d1e8ff 100%); padding: 20px; border-radius: 12px; margin: 20px 0; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; margin-top: 15px; }}
        .summary-item {{ background: white; padding: 15px; border-radius: 8px; text-align: center; }}
        .summary-number {{ font-size: 2em; font-weight: bold; color: #1a73e8; }}
        .section {{ background: white; padding: 20px; border-radius: 12px; margin: 15px 0; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
        .article {{ border-left: 4px solid #1a73e8; padding: 12px 15px; margin: 12px 0; background: #fafafa; border-radius: 0 8px 8px 0; transition: all 0.2s; }}
        .article:hover {{ background: #f0f7ff; transform: translateX(3px); }}
        .article.high-relevance {{ border-left-color: #ea4335; background: #fff5f5; }}
        .article.shortage-alert {{ border-left-color: #ff9800; background: #fff8e1; }}
        .article.shortage-ok {{ border-left-color: #34a853; background: #f1f8e9; }}
        .article.new-article {{ border-left-color: #9c27b0; background: #f3e5f5; }}
        .article-title {{ font-weight: bold; color: #1a73e8; text-decoration: none; font-size: 1.05em; }}
        .article-title:hover {{ text-decoration: underline; }}
        .meta {{ color: #666; font-size: 0.9em; margin-top: 8px; }}
        .badge {{ display: inline-block; padding: 3px 10px; border-radius: 15px; font-size: 0.8em; margin-right: 6px; margin-top: 5px; }}
        .badge-new {{ background: #9c27b0; color: white; font-weight: bold; }}
        .badge-dosage {{ background: #e3f2fd; color: #1565c0; }}
        .badge-status {{ background: #f3e5f5; color: #7b1fa2; }}
        .badge-score {{ background: #fff3e0; color: #e65100; }}
        .badge-source {{ background: #e8f5e9; color: #2e7d32; }}
        .keywords {{ margin-top: 25px; padding: 20px; background: #fafafa; border-radius: 12px; }}
        .keyword-tag {{ display: inline-block; background: #e0e0e0; padding: 4px 12px; border-radius: 20px; margin: 4px; font-size: 0.85em; }}
        .footer {{ text-align: center; color: #888; margin-top: 40px; padding: 20px; border-top: 1px solid #ddd; }}
        .toc {{ background: #f8f9fa; padding: 15px 20px; border-radius: 8px; margin-bottom: 20px; }}
        .toc a {{ color: #1a73e8; text-decoration: none; margin-right: 15px; }}
        .toc a:hover {{ text-decoration: underline; }}
        
        /* 差分セクション用スタイル */
        .diff-section {{ background: linear-gradient(135deg, #fff9e6 0%, #fff3cd 100%); padding: 20px; border-radius: 12px; margin: 20px 0; border: 2px solid #ffc107; }}
        .diff-section.first-run {{ background: #f8f9fa; border-color: #dee2e6; }}
        .diff-section.no-changes {{ background: #d4edda; border-color: #28a745; }}
        .diff-meta {{ color: #666; font-size: 0.9em; margin-bottom: 15px; }}
        .diff-summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; margin: 15px 0; }}
        .diff-item {{ background: white; padding: 15px; border-radius: 8px; text-align: center; border: 1px solid #ddd; }}
        .diff-item.highlight {{ border-color: #28a745; background: #d4edda; }}
        .diff-item.highlight-warning {{ border-color: #dc3545; background: #f8d7da; }}
        .diff-number {{ display: block; font-size: 1.8em; font-weight: bold; color: #333; }}
        .diff-label {{ font-size: 0.85em; color: #666; }}
        .diff-detail {{ margin-top: 15px; padding: 15px; background: white; border-radius: 8px; }}
        .diff-detail h4 {{ margin: 0 0 10px 0; color: #333; }}
        .change-item {{ padding: 8px 12px; margin: 5px 0; border-radius: 5px; }}
        .change-item.alert-high {{ background: #f8d7da; color: #721c24; }}
        .change-item.alert-info {{ background: #d4edda; color: #155724; }}
        .new-item {{ padding: 8px 0; border-bottom: 1px solid #eee; }}
        .new-item:last-child {{ border-bottom: none; }}
        .new-badge {{ display: inline-block; background: #9c27b0; color: white; padding: 2px 8px; border-radius: 10px; font-size: 0.7em; margin-right: 8px; font-weight: bold; }}
        .source-tag {{ color: #666; font-size: 0.85em; margin-left: 10px; }}
        .more-items {{ color: #666; font-style: italic; margin-top: 10px; }}
        .no-changes-msg {{ padding: 20px; text-align: center; color: #155724; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔬 GLP-1 Diet Trends Survey Report</h1>
        <p>生成日時: {now}</p>
        
        <div class="toc">
            <strong>📑 目次:</strong>
            <a href="#diff">差分</a>
            <a href="#summary">サマリー</a>
            <a href="#shortage">Drug Shortage</a>
            <a href="#fda">FDA承認/申請</a>
            <a href="#articles">収集記事</a>
            <a href="#keywords">検索キーワード</a>
        </div>
        
        {diff_html}
        
        <div class="summary" id="summary">
            <strong>📊 サマリー</strong>
            <div class="summary-grid">
                <div class="summary-item">
                    <div class="summary-number">{len(articles)}</div>
                    <div>収集記事数</div>
                </div>
                <div class="summary-item">
                    <div class="summary-number">{len(fda_approvals)}</div>
                    <div>FDA承認/申請</div>
                </div>
                <div class="summary-item">
                    <div class="summary-number">{len(shortage_articles)}</div>
                    <div>Shortage監視</div>
                </div>
            </div>
        </div>
"""
        
        # Drug Shortage セクション
        html += """
        <div class="section" id="shortage">
            <h2>📦 Drug Shortage 監視状況</h2>
"""
        if shortage_articles:
            for article in shortage_articles:
                css_class = "shortage-alert" if "⚠️" in article.title else "shortage-ok"
                new_badge = '<span class="badge badge-new">NEW</span>' if article.is_new else ''
                html += f"""
            <div class="article {css_class}">
                {new_badge}
                <a class="article-title" href="{article.url}" target="_blank">{article.title}</a>
                <div class="meta">{article.summary or ''}</div>
            </div>
"""
        else:
            html += "<p>監視対象薬剤の情報はありません。</p>"
        html += "        </div>\n"
        
        # FDA承認/申請情報セクション
        html += """
        <div class="section" id="fda">
            <h2>💊 FDA 承認・申請情報</h2>
"""
        if fda_approvals:
            for approval in fda_approvals[:30]:
                new_badge = '<span class="badge badge-new">NEW</span>' if approval.is_new else ''
                status_badge = f'<span class="badge badge-status">{approval.submission_status}</span>' if approval.submission_status else ''
                dosage_badge = f'<span class="badge badge-dosage">{approval.dosage_form}</span>' if approval.dosage_form else ''
                
                html += f"""
            <div class="article{'  new-article' if approval.is_new else ''}">
                {new_badge}
                <a class="article-title" href="{approval.url or '#'}" target="_blank">{approval.brand_name or approval.drug_name}</a>
                {status_badge} {dosage_badge}
                <div class="meta">
                    {f'適応: {approval.indication[:80]}...' if approval.indication and len(approval.indication) > 80 else f'適応: {approval.indication}' if approval.indication else ''}
                    {f' | 申請者: {approval.sponsor}' if approval.sponsor else ''}
                    {f' | 投与経路: {approval.route}' if approval.route else ''}
                </div>
            </div>
"""
        else:
            html += "<p>FDA承認/申請情報はありません。</p>"
        html += "        </div>\n"
        
        # カテゴリ別記事セクション
        html += """
        <div class="section" id="articles">
            <h2>📰 収集記事</h2>
"""
        categories = self.config.get("categories", default={})
        articles_by_category = {}
        for article in articles:
            cat = article.category
            if cat not in articles_by_category:
                articles_by_category[cat] = []
            articles_by_category[cat].append(article)
        
        for cat_key, cat_info in sorted(categories.items(), key=lambda x: x[1].get("priority", 99)):
            if cat_key not in articles_by_category:
                continue
            
            cat_articles = articles_by_category[cat_key]
            cat_name = cat_info.get("display_name", cat_key)
            new_count = sum(1 for a in cat_articles if a.is_new)
            
            html += f"""
            <h3>{cat_name} ({len(cat_articles)}件{f', 新規{new_count}件' if new_count > 0 else ''})</h3>
"""
            for article in sorted(cat_articles, key=lambda x: (not x.is_new, -x.relevance_score))[:20]:
                css_class = "new-article" if article.is_new else ("high-relevance" if article.relevance_score >= 5 else "")
                new_badge = '<span class="badge badge-new">NEW</span>' if article.is_new else ''
                score_badge = f'<span class="badge badge-score">Score: {article.relevance_score:.1f}</span>'
                dosage_badge = f'<span class="badge badge-dosage">{article.dosage_form}</span>' if article.dosage_form else ''
                source_badge = f'<span class="badge badge-source">{article.source}</span>'
                
                html += f"""
            <div class="article {css_class}">
                {new_badge}
                <a class="article-title" href="{article.url}" target="_blank">{article.title}</a>
                <div>{score_badge} {dosage_badge} {source_badge}</div>
                <div class="meta">
                    {article.published_date or '日付不明'}
                    {f' | 一致: {", ".join(article.matched_terms[:5])}' if article.matched_terms else ''}
                </div>
                {f'<p style="margin-top:8px;color:#555;">{article.summary[:200]}...</p>' if article.summary and len(article.summary) > 200 else f'<p style="margin-top:8px;color:#555;">{article.summary}</p>' if article.summary else ''}
            </div>
"""
        html += "        </div>\n"
        
        # 検索キーワード表示
        html += """
        <div class="keywords" id="keywords">
            <h3>🔍 使用した検索キーワード</h3>
"""
        search_terms = self.config.get("search_terms", default={})
        
        indications = search_terms.get("indications", {})
        if indications:
            html += "<p><strong>適応症:</strong> "
            for ind_key, ind_data in indications.items():
                for alias in ind_data.get("aliases", [])[:3]:
                    html += f'<span class="keyword-tag">{alias}</span>'
            html += "</p>\n"
        
        drug_names = search_terms.get("drug_names", {})
        if drug_names:
            html += "<p><strong>薬品名:</strong> "
            for drug_key in list(drug_names.keys())[:10]:
                html += f'<span class="keyword-tag">{drug_key}</span>'
            html += "</p>\n"
        
        html += """
        </div>
        
        <div class="footer">
            <p>Generated by GLP-1 Survey Tool v4.3 (差分機能付き)</p>
            <p>Data sources: FDA, WHO, Novo Nordisk, Eli Lilly, Industry News</p>
        </div>
    </div>
</body>
</html>
"""
        return html
    
    def _generate_diff_section_html(self, diff: Dict[str, Any]) -> str:
        """差分セクションのHTMLを生成"""
        if not diff:
            return ""
        
        if not diff.get('has_previous'):
            return """
        <div class="diff-section first-run" id="diff">
            <h2>🔄 差分情報</h2>
            <p>ℹ️ 初回実行のため、差分情報はありません。次回実行時から差分が表示されます。</p>
        </div>
"""
        
        prev_time_str = diff.get('previous_timestamp_display', '')
        new_count = diff.get('new_article_count', 0)
        shortage_changes = diff.get('shortage_changes', [])
        new_fda = diff.get('new_fda_approvals', [])
        articles_delta = diff.get('summary', {}).get('articles_delta', 0)
        
        has_changes = new_count > 0 or len(shortage_changes) > 0 or len(new_fda) > 0
        section_class = "" if has_changes else "no-changes"
        
        html = f"""
        <div class="diff-section {section_class}" id="diff">
            <h2>🔄 前回からの変更点</h2>
            <p class="diff-meta">前回実行: {prev_time_str}</p>
            
            <div class="diff-summary">
                <div class="diff-item {'highlight' if new_count > 0 else ''}">
                    <span class="diff-number">{new_count}</span>
                    <span class="diff-label">新規記事</span>
                </div>
                <div class="diff-item {'highlight-warning' if len(shortage_changes) > 0 else ''}">
                    <span class="diff-number">{len(shortage_changes)}</span>
                    <span class="diff-label">Shortage変化</span>
                </div>
                <div class="diff-item {'highlight' if len(new_fda) > 0 else ''}">
                    <span class="diff-number">{len(new_fda)}</span>
                    <span class="diff-label">新規FDA情報</span>
                </div>
                <div class="diff-item">
                    <span class="diff-number">{'+' if articles_delta >= 0 else ''}{articles_delta}</span>
                    <span class="diff-label">記事数増減</span>
                </div>
            </div>
"""
        
        # Shortage変化の詳細
        if shortage_changes:
            html += """
            <div class="diff-detail">
                <h4>⚠️ Drug Shortage ステータス変化</h4>
"""
            for change in shortage_changes:
                severity_class = "alert-high" if change['severity'] == 'high' else "alert-info"
                html += f"""
                <div class="change-item {severity_class}">{change['message']}</div>
"""
            html += "            </div>\n"
        
        # 新規記事の詳細
        if new_count > 0:
            new_articles = diff.get('new_articles', [])
            html += f"""
            <div class="diff-detail">
                <h4>📰 新規記事 (上位{min(10, new_count)}件)</h4>
"""
            for article in new_articles[:10]:
                html += f"""
                <div class="new-item">
                    <span class="new-badge">NEW</span>
                    <a href="{article.url}" target="_blank">{article.title[:80]}{'...' if len(article.title) > 80 else ''}</a>
                    <span class="source-tag">{article.source}</span>
                </div>
"""
            if new_count > 10:
                html += f"<p class='more-items'>...他 {new_count - 10}件</p>"
            html += "            </div>\n"
        
        # 新規FDA情報
        if new_fda:
            html += """
            <div class="diff-detail">
                <h4>💊 新規FDA承認/申請情報</h4>
"""
            for approval in new_fda[:5]:
                html += f"""
                <div class="new-item">
                    <span class="new-badge">NEW</span>
                    <strong>{approval.brand_name or approval.drug_name}</strong>
                    - {approval.submission_status}
                    {f'[{approval.dosage_form}]' if approval.dosage_form else ''}
                </div>
"""
            html += "            </div>\n"
        
        if not has_changes:
            html += """
            <div class="no-changes-msg">
                ✅ 前回から大きな変更はありません
            </div>
"""
        
        html += "        </div>\n"
        return html
    
    def _generate_markdown(self, articles: List[Article], shortage_articles: List[Article],
                           fda_approvals: List[DrugApproval], diff: Dict[str, Any] = None) -> str:
        """Markdownレポートを生成"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        
        md = f"""# GLP-1 Diet Trends Survey Report
生成日時: {now}

## 📊 サマリー
- 収集記事数: {len(articles)}件
- FDA承認/申請情報: {len(fda_approvals)}件
- Drug Shortage監視: {len(shortage_articles)}件

"""
        
        # 差分セクション
        if diff:
            if diff.get('has_previous'):
                md += f"""## 🔄 前回からの変更点
前回実行: {diff.get('previous_timestamp_display', '')}

| 項目 | 件数 |
|------|------|
| 新規記事 | {diff.get('new_article_count', 0)}件 |
| Shortage変化 | {len(diff.get('shortage_changes', []))}件 |
| 新規FDA情報 | {len(diff.get('new_fda_approvals', []))}件 |

"""
                for change in diff.get('shortage_changes', []):
                    md += f"- {change['message']}\n"
                
                if diff.get('new_articles'):
                    md += "\n### 新規記事（上位10件）\n"
                    for article in diff['new_articles'][:10]:
                        md += f"- **[NEW]** [{article.title}]({article.url}) - {article.source}\n"
            else:
                md += "## 🔄 差分情報\nℹ️ 初回実行のため差分情報はありません。\n\n"
        
        md += """---

## 📦 Drug Shortage 監視状況
"""
        for article in shortage_articles:
            status = "🟢" if "✅" in article.title else "🟠"
            new_mark = " **[NEW]**" if article.is_new else ""
            md += f"\n{status}{new_mark} **{article.title}**\n"
            md += f"   {article.summary}\n"
        
        md += "\n---\n\n## 💊 FDA 承認・申請情報\n"
        for approval in fda_approvals[:20]:
            new_mark = " **[NEW]**" if approval.is_new else ""
            md += f"\n### {approval.brand_name or approval.drug_name}{new_mark}\n"
            md += f"- ステータス: {approval.submission_status}\n"
            if approval.dosage_form:
                md += f"- 剤形: {approval.dosage_form}\n"
            if approval.indication:
                md += f"- 適応: {approval.indication[:100]}...\n"
            if approval.url:
                md += f"- [詳細]({approval.url})\n"
        
        md += "\n---\n\n## 📰 収集記事\n"
        for article in sorted(articles, key=lambda x: (not x.is_new, -x.relevance_score))[:30]:
            new_mark = " **[NEW]**" if article.is_new else ""
            md += f"\n### [{article.title}]({article.url}){new_mark}\n"
            md += f"- ソース: {article.source} | カテゴリ: {article.category}\n"
            md += f"- 関連度スコア: {article.relevance_score:.1f}\n"
            if article.dosage_form:
                md += f"- 剤形: {article.dosage_form}\n"
            if article.summary:
                md += f"\n{article.summary[:200]}...\n"
        
        md += "\n---\n\n*Generated by GLP-1 Survey Tool v4.3*\n"
        return md
    
    def _generate_json(self, articles: List[Article], shortage_articles: List[Article],
                       fda_approvals: List[DrugApproval], diff: Dict[str, Any] = None) -> str:
        """JSONレポートを生成"""
        data = {
            "generated_at": datetime.now().isoformat(),
            "version": "4.3",
            "summary": {
                "total_articles": len(articles),
                "new_articles": sum(1 for a in articles if a.is_new),
                "fda_approvals": len(fda_approvals),
                "shortage_checks": len(shortage_articles)
            },
            "diff": {
                "has_previous": diff.get('has_previous', False) if diff else False,
                "previous_timestamp": diff.get('previous_timestamp') if diff else None,
                "new_article_count": diff.get('new_article_count', 0) if diff else 0,
                "shortage_changes": diff.get('shortage_changes', []) if diff else [],
                "new_fda_count": len(diff.get('new_fda_approvals', [])) if diff else 0
            },
            "shortage_status": [
                {
                    "title": a.title,
                    "summary": a.summary,
                    "url": a.url,
                    "is_new": a.is_new
                } for a in shortage_articles
            ],
            "fda_approvals": [
                {
                    "drug_name": a.drug_name,
                    "brand_name": a.brand_name,
                    "status": a.submission_status,
                    "dosage_form": a.dosage_form,
                    "route": a.route,
                    "indication": a.indication,
                    "sponsor": a.sponsor,
                    "url": a.url,
                    "is_new": a.is_new
                } for a in fda_approvals
            ],
            "articles": [
                {
                    "title": a.title,
                    "url": a.url,
                    "source": a.source,
                    "category": a.category,
                    "published_date": a.published_date,
                    "summary": a.summary,
                    "relevance_score": a.relevance_score,
                    "matched_terms": a.matched_terms,
                    "dosage_form": a.dosage_form,
                    "is_new": a.is_new
                } for a in articles
            ]
        }
        return json.dumps(data, ensure_ascii=False, indent=2)


# ============================================================
# メインマネージャー
# ============================================================

class GLP1SurveyManager:
    """GLP-1サーベイ統合マネージャー"""
    
    def __init__(self, config_path: str = "/content/glp1_config.json"):
        self.config = ConfigLoader(config_path)
        self.matcher = RelevanceMatcher(self.config)
        self.rss_fetcher = RSSFetcher(self.config, self.matcher)
        self.web_scraper = WebScraper(self.config, self.matcher)
        self.fda_client = FDAApiClient(self.config, self.matcher)
        self.shortage_monitor = DrugShortageMonitor(self.config, self.matcher)
        self.novel_approvals_scraper = NovelDrugApprovalsScraper(self.config, self.matcher)
        self.warning_letters_scraper = WarningLettersScraper(self.config, self.matcher)
        self.report_generator = ReportGenerator(self.config)
        
        self.output_dir = Path(self.config.get("general", "output_dir", default="/content/survey_output"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.seen_urls: Set[str] = self._load_seen_urls()
        self.diff_manager = DiffManager(self.output_dir, self.config)
    
    def _load_seen_urls(self) -> Set[str]:
        """既出URLを読み込み"""
        seen_file = self.output_dir / self.config.get("general", "seen_urls_file", default="seen_urls.json")
        try:
            if seen_file.exists():
                with open(seen_file, 'r') as f:
                    return set(json.load(f))
        except Exception:
            pass
        return set()
    
    def _save_seen_urls(self):
        """既出URLを保存"""
        seen_file = self.output_dir / self.config.get("general", "seen_urls_file", default="seen_urls.json")
        try:
            with open(seen_file, 'w') as f:
                json.dump(list(self.seen_urls), f)
        except Exception:
            pass
    
    def run_survey(self, include_seen: bool = False, output_format: str = "html") -> Dict[str, Any]:
        """サーベイを実行"""
        print("=" * 60)
        print("🔬 GLP-1 Diet Trends Survey v4.3 (差分機能付き)")
        print("=" * 60)
        
        all_articles = []
        fda_approvals = []
        
        # 1. Drug Shortage チェック
        print("\n📦 Drug Shortage 監視中...")
        shortage_articles = self.shortage_monitor.check_shortages()
        
        # 2. FDA API から承認/申請情報を取得
        print("\n💊 FDA API から承認/申請情報を取得中...")
        fda_approvals = self.fda_client.search_obesity_diabetes_drugs()
        print(f"    → {len(fda_approvals)}件")
        
        # 3. 各ソースから記事を収集
        sources = self.config.get("sources", default=[])
        enabled_sources = [s for s in sources if s.get("enabled", True)]
        
        print(f"\n📡 {len(enabled_sources)}件のソースから記事を収集中...")
        
        for source_dict in enabled_sources:
            source = SourceConfig(
                name=source_dict.get("name", "Unknown"),
                url=source_dict.get("url", ""),
                category=source_dict.get("category", "other"),
                source_type=source_dict.get("source_type", "web"),
                enabled=source_dict.get("enabled", True),
                priority=source_dict.get("priority", 2),
                rss_url=source_dict.get("rss_url"),
                subcategory=source_dict.get("subcategory"),
                selectors=source_dict.get("selectors"),
                dynamic_year=source_dict.get("dynamic_year", False)
            )
            
            print(f"  📥 {source.name}...", end="", flush=True)
            
            try:
                if source.subcategory == "novel_approvals":
                    articles = self.novel_approvals_scraper.fetch_articles(source)
                elif source.subcategory == "warning_letters":
                    articles = self.warning_letters_scraper.fetch_articles(source)
                elif source.subcategory == "drug_shortages":
                    print(" (Shortage監視で処理済み)")
                    continue
                elif source.source_type == "rss":
                    articles = self.rss_fetcher.fetch_articles(source)
                else:
                    articles = self.web_scraper.fetch_articles(source)
                
                if not include_seen:
                    articles = [a for a in articles if a.url not in self.seen_urls]
                
                for a in articles:
                    self.seen_urls.add(a.url)
                
                all_articles.extend(articles)
                print(f" → {len(articles)}件")
            except Exception as e:
                print(f" ⚠️ エラー: {str(e)[:40]}")
        
        # 4. 差分計算
        print("\n🔄 前回との差分を計算中...")
        diff = self.diff_manager.get_diff(all_articles, shortage_articles, fda_approvals)
        
        if diff['has_previous']:
            print(f"    前回実行: {diff['previous_timestamp_display']}")
            print(f"    新規記事: {diff['new_article_count']}件")
            if diff['shortage_changes']:
                print(f"    ⚠️ Shortage変化: {len(diff['shortage_changes'])}件")
                for change in diff['shortage_changes']:
                    print(f"      {change['message']}")
            if diff['new_fda_approvals']:
                print(f"    新規FDA情報: {len(diff['new_fda_approvals'])}件")
        else:
            print("    ℹ️ 初回実行（差分なし）")
        
        # 5. スナップショット作成・保存
        self.diff_manager.create_current_snapshot(all_articles, shortage_articles, fda_approvals)
        self.diff_manager.save_current_snapshot()
        
        # 6. レポート生成
        print(f"\n📝 レポートを生成中 (形式: {output_format})...")
        
        report_content = self.report_generator.generate(
            all_articles, shortage_articles, fda_approvals, output_format, diff
        )
        
        # 7. ファイル保存
        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        ext_map = {"html": "html", "markdown": "md", "json": "json"}
        ext = ext_map.get(output_format, "html")
        output_file = self.output_dir / f"glp1_survey_{timestamp}.{ext}"
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(report_content)
        
        print(f"    → 保存完了: {output_file}")
        
        # 8. 既出URL保存
        self._save_seen_urls()
        
        # 9. サマリー表示
        print("\n" + "=" * 60)
        print("📊 サーベイ完了サマリー")
        print("=" * 60)
        print(f"  📰 収集記事数: {len(all_articles)}件 (新規: {diff['new_article_count']}件)")
        print(f"  💊 FDA承認/申請情報: {len(fda_approvals)}件")
        print(f"  📦 Drug Shortage監視: {len(shortage_articles)}件")
        if diff['shortage_changes']:
            print(f"  ⚠️ Shortage状態変化: {len(diff['shortage_changes'])}件")
        print(f"  📁 出力ファイル: {output_file}")
        print("=" * 60)
        
        return {
            "articles": all_articles,
            "fda_approvals": fda_approvals,
            "shortage_articles": shortage_articles,
            "diff": diff,
            "output_file": str(output_file),
            "report_content": report_content
        }


# ============================================================
# 実行用関数
# ============================================================

def run_survey(config_path: str = "/content/glp1_config.json", 
               output_format: str = "html",
               include_seen: bool = False) -> Dict[str, Any]:
    """サーベイを実行するヘルパー関数"""
    manager = GLP1SurveyManager(config_path)
    return manager.run_survey(include_seen=include_seen, output_format=output_format)


def search_specific_drug(drug_name: str, config_path: str = "/content/glp1_config.json"):
    """特定の薬剤を検索"""
    config = ConfigLoader(config_path)
    matcher = RelevanceMatcher(config)
    fda_client = FDAApiClient(config, matcher)
    
    print(f"\n🔍 {drug_name} を検索中...")
    results = fda_client.search_by_drug_name(drug_name)
    
    if results:
        print(f"\n✅ {len(results)}件の結果が見つかりました:\n")
        for r in results:
            print(f"  💊 {r.brand_name or r.drug_name}")
            print(f"     ステータス: {r.submission_status}")
            if r.dosage_form:
                print(f"     剤形: {r.dosage_form}")
            if r.route:
                print(f"     投与経路: {r.route}")
            if r.sponsor:
                print(f"     申請者: {r.sponsor}")
            if r.url:
                print(f"     URL: {r.url}")
            print()
    else:
        print(f"  ℹ️ {drug_name} に関する情報は見つかりませんでした")
    
    return results


def check_drug_shortage(drug_name: str, config_path: str = "/content/glp1_config.json"):
    """特定薬剤のDrug Shortage状況を確認"""
    config = ConfigLoader(config_path)
    matcher = RelevanceMatcher(config)
    monitor = DrugShortageMonitor(config, matcher)
    
    print(f"\n📦 {drug_name} のDrug Shortage状況を確認中...")
    shortage_list = monitor._fetch_shortage_list()
    
    search_term = drug_name.lower()
    found = None
    for key, data in shortage_list.items():
        if search_term in key or search_term in data['name'].lower():
            found = data
            break
    
    if found:
        if found['in_shortage']:
            print(f"\n⚠️ 供給不足: {found['name']}")
            print(f"   ステータス: {found['status']}")
        else:
            print(f"\n✅ 供給解消: {found['name']}")
            print(f"   ステータス: {found['status']}")
        print(f"   詳細: {found['url']}")
    else:
        print(f"\n✅ {drug_name} はDrug Shortageリストに記載がありません（供給正常）")
    
    return found


def show_last_diff(config_path: str = "/content/glp1_config.json"):
    """前回の差分情報を表示"""
    config = ConfigLoader(config_path)
    output_dir = Path(config.get("general", "output_dir", default="/content/survey_output"))
    snapshot_file = output_dir / config.get("general", "snapshot_file", default="last_snapshot.json")
    
    if not snapshot_file.exists():
        print("ℹ️ スナップショットファイルがありません（未実行）")
        return None
    
    try:
        with open(snapshot_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        print("\n📂 前回のスナップショット情報:")
        print(f"   実行日時: {data.get('timestamp', 'N/A')}")
        print(f"   記事数: {data.get('article_count', 0)}件")
        print(f"   FDA情報: {data.get('fda_count', 0)}件")
        print(f"   Shortage監視: {data.get('shortage_count', 0)}件")
        
        shortage_status = data.get('shortage_status', {})
        if shortage_status:
            print("\n   Shortage状態:")
            for drug, status in shortage_status.items():
                emoji = "⚠️" if status == "shortage" else "✅"
                print(f"     {emoji} {drug}: {status}")
        
        return data
    except Exception as e:
        print(f"⚠️ スナップショット読み込みエラー: {e}")
        return None


# ============================================================
# メイン実行
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("🔬 GLP-1 Survey Tool v4.3 (差分機能付き)")
    print("   設定ファイル分離版 - Google Colab対応")
    print("=" * 60)
    print("\n使用方法:")
    print("  # サーベイ実行")
    print("  results = run_survey()")
    print("  results = run_survey(output_format='markdown')")
    print("  results = run_survey(include_seen=True)  # 既出含む")
    print()
    print("  # 特定薬剤の検索")
    print("  search_specific_drug('semaglutide')")
    print("  search_specific_drug('wegovy')")
    print()
    print("  # Drug Shortage確認")
    print("  check_drug_shortage('liraglutide')")
    print()
    print("  # 前回の差分情報を表示")
    print("  show_last_diff()")
    print("=" * 60)
