import discord
from discord.ext import tasks, commands
import pandas as pd
import numpy as np
import requests
import csv
import re
import os
import io
import time
from datetime import datetime, timedelta, timezone

# --- CONFIGURATION ---
USER_API_URL = "https://xtracker.polymarket.com/api/users/elonmusk"
POSTS_API_URL = "https://xtracker.polymarket.com/api/users/elonmusk/posts"
TRACKING_API_BASE = "https://xtracker.polymarket.com/api/trackings"
OLD_DATA_URL = "https://www.xtracker.io/api/download"

# Config
TARGET_HANDLE = "elonmusk"
TIMEZONE_OFFSET = -5  # EST
TIMEZONE_OFFSET_MYT = 8 # MYT (GMT+8)
FORCE_YEAR = 2025 

# --- DISCORD CONFIG ---
DISCORD_TOKEN = 'YOUR_DISCORD_BOT_TOKEN_HERE' 
CHANNEL_ID = 123456789012345678

class ElonAnalyticsEngine:
    """
    Master Predictor Engine v3.4 (Cycle Alignment)
    Features: Aligns daily counts to Market Start Time (e.g. 5pm-5pm UTC) for 100% accuracy.
    """
    def __init__(self):
        self.df = None
        self.now_utc = None
        self.now_est = None
        self.now_myt = None
        
        self.live_csv = 'elon_tweets_live.csv'
        self.old_api_csv = 'old_elonmusk_tweet.csv'
        self.history_csv = 'elon_tweets_history.csv'
        self.archived_markets_file = 'archived_market_ids.txt'
        
        self.archived_ids = set()
        if os.path.exists(self.archived_markets_file):
            with open(self.archived_markets_file, 'r') as f:
                self.archived_ids = set(line.strip() for line in f if line.strip())
        
        self.active_markets = {}
        
        self.hourly_stats = {}
        self.behavior_matrix = {}
        self.hourly_stats_recent = {} 
        self.behavior_matrix_recent = {} 
        
        self.velocity_6h = 0.0
        self.velocity_24h = 0.0
        self.velocity_72h = 0.0
        self.daily_std_dev = 0

    def log(self, text):
        return text + "\n"

    def update_time(self):
        self.now_utc = datetime.now(timezone.utc)
        self.now_est = self.now_utc.astimezone(timezone(timedelta(hours=TIMEZONE_OFFSET)))
        self.now_myt = self.now_utc.astimezone(timezone(timedelta(hours=TIMEZONE_OFFSET_MYT)))

    def parse_iso_date(self, date_str):
        if not date_str: return None
        try:
            dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            return dt
        except Exception:
            try:
                dt = datetime.strptime(date_str.split('.')[0], "%Y-%m-%dT%H:%M:%S")
                return dt.replace(tzinfo=timezone.utc)
            except:
                return None

    def parse_csv_date(self, date_str):
        if not isinstance(date_str, str) or len(date_str) < 5: return None
        try:
            clean_str = re.sub(r'\s+[A-Z]{3}\s*$', '', date_str).strip()
            if str(FORCE_YEAR) not in clean_str:
                clean_str_with_year = f"{clean_str}, {FORCE_YEAR}"
                dt = datetime.strptime(clean_str_with_year, '%b %d, %I:%M:%S %p, %Y')
            else:
                try: dt = datetime.strptime(clean_str, '%Y-%m-%d %H:%M:%S')
                except: dt = datetime.strptime(clean_str, '%b %d, %Y, %I:%M:%S %p')
            
            est_tz = timezone(timedelta(hours=TIMEZONE_OFFSET))
            dt = dt.replace(tzinfo=est_tz)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def validate_market_integrity(self, title, api_start_dt):
        try:
            months = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}
            pattern = r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*(?:\s+(\d{1,2}))?'
            matches = re.findall(pattern, title, re.IGNORECASE)
            if not matches: return True
            month_str, day_str = matches[0]
            month_idx = months.get(month_str.lower()[:3])
            if not month_idx: return True
            day_idx = int(day_str) if day_str else 1
            api_date = api_start_dt.date()
            base_year = api_start_dt.year
            candidates = []
            for y_offset in [-1, 0, 1]:
                try:
                    candidate = api_start_dt.replace(year=base_year + y_offset, month=month_idx, day=day_idx, hour=0, minute=0, second=0, microsecond=0).date()
                    candidates.append(abs((api_date - candidate).days))
                except ValueError: continue
            min_diff = min(candidates) if candidates else 0
            if min_diff > 5: return False
            return True
        except Exception: return True

    def check_market_warning(self, title, api_start_dt):
        if not self.validate_market_integrity(title, api_start_dt):
            print(f"⚠️ Metadata Warning: '{title}' dates might be mismatched, but tracking anyway (Active=True).")

    def fetch_and_archive_market(self, market_id, title, start_dt, end_dt):
        url = f"{TRACKING_API_BASE}/{market_id}?includeStats=true"
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200: return
            data = resp.json().get('data', {})
            stats = data.get('stats', {})
            hourly_buckets = stats.get('daily', [])
            new_rows = []
            for bucket in hourly_buckets:
                date_str = bucket.get('date')
                count = int(bucket.get('count', 0))
                if count > 0 and date_str:
                    base_dt = self.parse_iso_date(date_str)
                    if base_dt and (start_dt <= base_dt <= end_dt):
                        interval = 3600 / count
                        for i in range(count):
                            tweet_dt = base_dt + timedelta(seconds=i*interval)
                            syn_id = f"hist_{market_id}_{int(tweet_dt.timestamp())}_{i}"
                            # Strict String ID
                            new_rows.append({'id': str(syn_id).strip(), 'created_at': tweet_dt})
            if new_rows:
                df_new = pd.DataFrame(new_rows)
                file_exists = os.path.exists(self.history_csv)
                df_new.to_csv(self.history_csv, mode='a', header=not file_exists, index=False)
                with open(self.archived_markets_file, 'a') as f: f.write(f"{market_id}\n")
                self.archived_ids.add(market_id)
        except Exception: pass

    def fetch_old_bulk_data(self):
        print("Fetching bulk history from API...")
        try:
            payload = {"handle": TARGET_HANDLE, "platform": "X"}
            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Origin': 'https://www.xtracker.io',
                'Referer': 'https://www.xtracker.io/'
            }
            resp = requests.post(OLD_DATA_URL, json=payload, headers=headers, timeout=45)
            if resp.status_code == 200 and "text/csv" in resp.headers.get('Content-Type', ''):
                with open(self.old_api_csv, 'w', encoding='utf-8') as f: f.write(resp.text)
                
                rows = []
                csv_reader = csv.reader(io.StringIO(resp.text))
                next(csv_reader, None)
                for row in csv_reader:
                    if not row: continue
                    t_id = row[0]
                    t_date = None
                    for cell in row:
                        parsed = self.parse_csv_date(cell)
                        if parsed:
                            t_date = parsed
                            break
                    if t_id and t_date:
                        # Normalize IDs to String here
                        rows.append({'id': str(t_id).strip(), 'created_at': t_date})
                return pd.DataFrame(rows)
            return pd.DataFrame()
        except Exception: return pd.DataFrame()

    def refresh_data(self):
        print("--- Refreshing Data ---")
        self.active_markets = {}
        seen_windows = []
        headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://xtracker.polymarket.com/'}

        # 1. Detect Markets
        try:
            resp = requests.get(USER_API_URL, headers=headers, timeout=15)
            if resp.status_code == 200:
                trackings = resp.json().get('data', {}).get('trackings', [])
                for t in trackings:
                    title = t.get('title')
                    tid = t.get('id')
                    start_str = t.get('startDate')
                    end_str = t.get('endDate')
                    is_active = t.get('isActive')
                    if title and tid and start_str and end_str:
                        s_dt = self.parse_iso_date(start_str)
                        e_dt = self.parse_iso_date(end_str)
                        if s_dt and e_dt:
                            if is_active:
                                self.check_market_warning(title, s_dt)
                                is_duplicate = False
                                for existing_s, existing_e in seen_windows:
                                    if abs((s_dt - existing_s).total_seconds()) < 7200 and \
                                       abs((e_dt - existing_e).total_seconds()) < 7200:
                                        is_duplicate = True; break
                                if not is_duplicate:
                                    self.active_markets[title] = (s_dt, e_dt)
                                    seen_windows.append((s_dt, e_dt))
                                    print(f"🔹 Active Market: {title}")
                            elif not is_active and e_dt < self.now_utc:
                                if tid not in self.archived_ids:
                                    if self.validate_market_integrity(title, s_dt):
                                        self.fetch_and_archive_market(tid, title, s_dt, e_dt)
        except Exception: pass

        # 2. Fetch Live Tweets
        df_live = pd.DataFrame()
        try:
            end_date_str = (self.now_utc + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            params = {'startDate': '2025-11-01T04:00:00.000Z', 'endDate': end_date_str}
            p_resp = requests.get(POSTS_API_URL, headers=headers, params=params, timeout=25)
            if p_resp.status_code == 200:
                posts_list = p_resp.json().get('data', {}).get('posts', [])
                if isinstance(p_resp.json().get('data'), list): posts_list = p_resp.json().get('data')
                live_rows = []
                for p in posts_list:
                    if p.get('id') and p.get('createdAt'):
                        dt = self.parse_iso_date(p['createdAt'])
                        if dt: 
                            # Strict String ID
                            live_rows.append({'id': str(p['id']).strip(), 'created_at': dt})
                df_live = pd.DataFrame(live_rows)
                if not df_live.empty: df_live.to_csv(self.live_csv, index=False)
            else:
                if os.path.exists(self.live_csv):
                    df_live = pd.read_csv(self.live_csv, dtype={'id': str})
                    df_live['created_at'] = pd.to_datetime(df_live['created_at'], utc=True)
        except Exception:
            if os.path.exists(self.live_csv):
                df_live = pd.read_csv(self.live_csv, dtype={'id': str})
                df_live['created_at'] = pd.to_datetime(df_live['created_at'], utc=True)

        # 3. Fetch/Load Old Bulk Data
        df_old = self.fetch_old_bulk_data()
        if df_old.empty and os.path.exists(self.old_api_csv):
            try:
                df_old = pd.read_csv(self.old_api_csv, dtype={'id': str})
                df_old['created_at'] = pd.to_datetime(df_old['created_at'], utc=True)
            except: pass

        # 4. Load History
        df_hist = pd.DataFrame()
        if os.path.exists(self.history_csv):
            try:
                df_hist = pd.read_csv(self.history_csv, dtype={'id': str})
                df_hist['created_at'] = pd.to_datetime(df_hist['created_at'], utc=True)
            except: pass

        # 5. SMART MERGE
        if not df_live.empty:
            min_live = df_live['created_at'].min()
            if not df_old.empty: df_old = df_old[df_old['created_at'] < min_live]
            if not df_hist.empty: df_hist = df_hist[df_hist['created_at'] < min_live]

        if not df_old.empty:
            min_old = df_old['created_at'].min()
            if not df_hist.empty: df_hist = df_hist[df_hist['created_at'] < min_old]

        full_df = pd.concat([df_live, df_old, df_hist])
        if not full_df.empty:
            full_df.sort_values('created_at', inplace=True)
            
            # FINAL DEDUPLICATION
            pre_len = len(full_df)
            full_df.drop_duplicates(subset=['id'], inplace=True)
            post_len = len(full_df)
            
            self.df = full_df
            self.df['est_time'] = self.df['created_at'].dt.tz_convert(timezone(timedelta(hours=TIMEZONE_OFFSET)))
            
            print(f"✅ Merged {post_len} tweets (Dropped {pre_len - post_len} duplicates).")
            self._calculate_stats()
        else:
            print("⚠️ No data available.")

    def _calculate_stats(self):
        if self.df is None or self.df.empty: return
        self.df['hour'] = self.df['est_time'].dt.hour
        self.df['dow'] = self.df['est_time'].dt.dayofweek
        
        start_recent = self.now_est - timedelta(days=14)
        df_recent = self.df[self.df['est_time'] >= start_recent].copy()
        if df_recent.empty: df_recent = self.df 
            
        unique_days_recent = max(1, df_recent['est_time'].dt.date.nunique())
        hourly_counts = df_recent.groupby('hour').size()
        self.hourly_stats_recent = {h: hourly_counts.get(h, 0) / unique_days_recent for h in range(24)}
        
        matrix_raw = df_recent.groupby(['dow', 'hour']).size()
        total_recent_tweets = len(df_recent)
        self.behavior_matrix_recent = (matrix_raw / max(1, total_recent_tweets)).to_dict()
        
        daily_counts = self.df.groupby(self.df['est_time'].dt.date).size()
        self.daily_std_dev = daily_counts.tail(30).std()
        if pd.isna(self.daily_std_dev): self.daily_std_dev = 5.0
        
        start_6h = self.now_est - timedelta(hours=6)
        start_24h = self.now_est - timedelta(hours=24) 
        start_72h = self.now_est - timedelta(hours=72)
        
        tweets_6h = len(self.df[self.df['est_time'] >= start_6h])
        tweets_24h = len(self.df[self.df['est_time'] >= start_24h])
        tweets_72h = len(self.df[self.df['est_time'] >= start_72h])
        
        self.velocity_6h = tweets_6h / 6.0
        self.velocity_24h = tweets_24h / 24.0
        self.velocity_72h = tweets_72h / 72.0

    def get_market_windows(self):
        return self.active_markets

    def get_market_bracket(self, val, duration_hours):
        v = int(val)
        if duration_hours > 480: # Monthly
            if v < 20: return "< 20", 0, 19
            if v >= 1400: return "1400+", 1400, 9999
            if v < 600:
                base = 20; step = 20
                bucket_idx = (v - base) // step
                low = base + (bucket_idx * step)
                return f"{low}–{low+step-1}", low, low+step-1
            else:
                base = 600; step = 40
                bucket_idx = (v - base) // step
                low = base + (bucket_idx * step)
                return f"{low}–{low+step-1}", low, low+step-1
        else: # Weekly
            if v < 20: return "< 20", 0, 19
            if v >= 500: return "500+", 500, 9999
            base = 20; step = 20
            bucket_idx = (v - base) // step
            low = base + (bucket_idx * step)
            return f"{low}–{low+step-1}", low, low+step-1

    # --- ALGORITHM 1 ---
    def run_algo_v1(self):
        out = "=== ALGORITHM 1: LINEAR BASELINE ===\n"
        markets = self.get_market_windows()
        if not markets: return out + "No active markets found."

        for name, (start_utc, end_utc) in markets.items():
            mask = (self.df['created_at'] >= start_utc) & (self.df['created_at'] <= self.now_utc)
            current_total = len(self.df[mask])
            elapsed_hours = (self.now_utc - start_utc).total_seconds() / 3600
            remaining_hours = max(0, (end_utc - self.now_utc).total_seconds() / 3600)
            total_duration = (end_utc - start_utc).total_seconds() / 3600
            
            rate = current_total / elapsed_hours if elapsed_hours > 0 else 0
            final_pred = current_total + (rate * remaining_hours)
            
            r_str, _, _ = self.get_market_bracket(final_pred, total_duration)
            out += f"\n[{name}]\n"
            out += f"Current: {current_total} | Remaining: {remaining_hours:.1f}h\n"
            out += f"Pace: {rate:.2f}/hr -> Prediction: {int(final_pred)} (Bet: {r_str})\n"
            
            # --- FIXED DAILY BREAKDOWN LOGIC ---
            # Group by 24h cycles relative to Market Start
            subset_df = self.df[mask].copy()
            if not subset_df.empty:
                # Calculate Day Number (0 = Day 1, 1 = Day 2, etc.)
                subset_df['market_day'] = ((subset_df['created_at'] - start_utc).dt.total_seconds() // 86400).astype(int)
                
                daily_counts = subset_df.groupby('market_day').size()
                
                out += "📅 Daily Breakdown (Cycle-Aligned):\n"
                for day_idx, count in daily_counts.items():
                    # Calculate date label for this day
                    day_start = start_utc + timedelta(days=day_idx)
                    day_end = day_start + timedelta(days=1)
                    
                    # Formatting
                    d_label = day_start.strftime('%b %d')
                    out += f"  Day {day_idx+1} ({d_label}): {count}\n"
        return out

    # --- ALGORITHM 2 ---
    def run_algo_v2(self):
        out = "=== ALGORITHM 2: ADAPTIVE VELOCITY ===\n"
        markets = self.get_market_windows()
        if not markets: return out + "Waiting..."

        sorted_hours = sorted(self.hourly_stats_recent.items(), key=lambda x: x[1], reverse=True)
        top_hours = [f"{h:02d}:00" for h, val in sorted_hours[:3]]
        
        is_burst = self.velocity_6h > (self.velocity_72h * 1.5)
        burst_status = "🚨 BURST MODE" if is_burst else "✅ Stable"
        
        current_hour = self.now_est.hour
        avg_now = self.hourly_stats_recent.get(current_hour, 0)
        
        out += f"Status: {burst_status}\n"
        out += f"Speed (6h): {self.velocity_6h:.2f}/hr | Speed (3d): {self.velocity_72h:.2f}/hr\n"
        out += f"⚡ Recent Peak Hours: {', '.join(top_hours)}\n"

        for name, (start_utc, end_utc) in markets.items():
            mask = (self.df['created_at'] >= start_utc) & (self.df['created_at'] <= self.now_utc)
            current_total = len(self.df[mask])
            remaining_hours = max(0, (end_utc - self.now_utc).total_seconds() / 3600)
            total_duration = (end_utc - start_utc).total_seconds() / 3600

            if is_burst:
                blended_rate = (self.velocity_6h * 0.7) + (self.velocity_72h * 0.3)
            else:
                blended_rate = (self.velocity_6h * 0.3) + (self.velocity_72h * 0.7)
            
            accumulated = 0
            sim_time = self.now_est
            end_est = end_utc.astimezone(timezone(timedelta(hours=TIMEZONE_OFFSET)))
            global_recent_avg = sum(self.hourly_stats_recent.values()) / 24.0
            if global_recent_avg == 0: global_recent_avg = 1.0 
            
            while sim_time < end_est:
                next_hour = sim_time.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                if next_hour > end_est: next_hour = end_est
                fraction = (next_hour - sim_time).total_seconds() / 3600.0
                
                hour_val = self.hourly_stats_recent.get(sim_time.hour, global_recent_avg)
                activity_multiplier = hour_val / global_recent_avg
                
                accumulated += (blended_rate * activity_multiplier * fraction)
                sim_time = next_hour
            
            final_pred = current_total + accumulated
            r_str, _, _ = self.get_market_bracket(final_pred, total_duration)
            
            out += f"\n[{name}]\n"
            out += f"Final: {int(final_pred)} (Bet: {r_str})\n"
        return out

    # --- ALGORITHM 3 ---
    def run_algo_v3(self):
        out = "=== ALGORITHM 3: PROBABILISTIC ===\n"
        markets = self.get_market_windows()
        if not markets: return out + "Data insufficient."

        out += f"Volatility: +/- {self.daily_std_dev:.1f}\n"
        
        for name, (start_utc, end_utc) in markets.items():
            mask = (self.df['created_at'] >= start_utc) & (self.df['created_at'] <= self.now_utc)
            current_total = len(self.df[mask])
            remaining_hours = max(0, (end_utc - self.now_utc).total_seconds() / 3600)
            total_duration = (end_utc - start_utc).total_seconds() / 3600
            
            current_intensity = self.velocity_72h
            proj_add = 0
            temp_t = self.now_est
            end_est = end_utc.astimezone(timezone(timedelta(hours=TIMEZONE_OFFSET)))
            BASE_PROB = 0.00595
            
            while temp_t < end_est:
                k = (temp_t.weekday(), temp_t.hour)
                prob = self.behavior_matrix_recent.get(k, BASE_PROB)
                multiplier = prob / BASE_PROB
                expected_tweets = current_intensity * multiplier
                proj_add += expected_tweets
                temp_t += timedelta(hours=1)
            
            final_pred = current_total + proj_add
            days_rem = remaining_hours / 24
            margin = self.daily_std_dev * np.sqrt(max(days_rem, 0.5))
            safe_min = final_pred - margin
            safe_max = final_pred + margin

            range_str, p_low, p_high = self.get_market_bracket(final_pred, total_duration)
            u_label, u_low, u_high = self.get_market_bracket(safe_max, total_duration)
            l_label, l_low, l_high = self.get_market_bracket(safe_min, total_duration)

            out += f"\n[{name}]\n"
            out += f"Projected: {int(final_pred)} (Range: {int(safe_min)}-{int(safe_max)})\n"
            out += f"PRIMARY: {range_str}\n"
            
            hedges = []
            if u_low > p_high: hedges.append(f"UPPER HEDGE: {u_label}")
            if l_high < p_low: hedges.append(f"LOWER HEDGE: {l_label}")
            if hedges: out += "HEDGING:\n" + "\n".join(hedges)
            else: out += "ADVICE: Strong conviction."
            out += "\n"
        return out

    # --- ALGORITHM 4 ---
    def run_algo_v4(self):
        out = "=== ALGORITHM 4: ALPHA SCANNER ===\n"
        start_7d = self.now_est - timedelta(days=7)
        tweets_7d = len(self.df[self.df['est_time'] >= start_7d])
        rate_7d = tweets_7d / (24*7)
        rate_24h = self.velocity_24h
        ratio = rate_24h / rate_7d if rate_7d > 0 else 1.0
        
        out += f"7-Day Baseline: {rate_7d:.2f}/hr\n"
        out += f"24-Hour Speed: {rate_24h:.2f}/hr\n"
        out += f"Momentum Ratio: {ratio:.2f}x\n"
        
        projected_next_week = rate_24h * 168 
        range_str, _, _ = self.get_market_bracket(projected_next_week, 168)
        
        out += f"🚀 NEXT WEEK TARGET: {int(projected_next_week)} ({range_str})\n"
        
        if ratio > 1.2: out += "STRATEGY: 📈 BULLISH. Accelerating.\n"
        elif ratio < 0.8: out += "STRATEGY: 📉 BEARISH. Decelerating.\n"
        else: out += "STRATEGY: ⚖️ NEUTRAL. Steady state.\n"
        return out

# --- CLIENT ---
class ElonPredictorBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.engine = ElonAnalyticsEngine()

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        if not self.monitor_task.is_running():
            self.monitor_task.start()

    @tasks.loop(minutes=15)
    async def monitor_task(self):
        channel = self.get_channel(CHANNEL_ID)
        if not channel: return
        print("Running Analysis Cycle...")
        try: await channel.purge(limit=100)
        except Exception: pass
        self.engine.update_time()
        self.engine.refresh_data()
        
        if self.engine.df is None or self.engine.df.empty: return

        report_v1 = self.engine.run_algo_v1()
        report_v2 = self.engine.run_algo_v2()
        report_v3 = self.engine.run_algo_v3()
        report_v4 = self.engine.run_algo_v4()
        
        ts_est = self.engine.now_est.strftime('%Y-%m-%d %H:%M EST')
        ts_myt = self.engine.now_myt.strftime('%H:%M MYT')
        
        try:
            await channel.send(f"**🚀 Elon Prediction Update - {ts_est} | {ts_myt}**")
            await channel.send(f"```yaml\n{report_v1}\n```")
            await channel.send(f"```yaml\n{report_v2}\n```")
            await channel.send(f"```yaml\n{report_v3}\n```")
            await channel.send(f"```yaml\n{report_v4}\n```")
            print("Reports sent.")
        except Exception as e: print(f"Error sending: {e}")

    @monitor_task.before_loop
    async def before_monitor(self):
        await self.wait_until_ready()

if __name__ == "__main__":
    intents = discord.Intents.default()
    client = ElonPredictorBot(intents=intents)
    client.run(DISCORD_TOKEN)
