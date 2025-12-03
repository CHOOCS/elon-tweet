import discord
from discord.ext import tasks
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
TIMEZONE_OFFSET = -5  # EST (New York)
TIMEZONE_OFFSET_MYT = 8 # MYT (Malaysia/China)

# --- DISCORD CONFIG ---
# REPLACE THESE VALUES
DISCORD_TOKEN = 'YOUR_DISCORD_BOT_TOKEN_HERE' 
CHANNEL_ID = 123456789012345678

class ElonAnalyticsEngine:
    """
    Master Predictor Engine v4.0 (Mean Reversion & Decay)
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
        
        # --- Stats Containers ---
        # Stores the relative weight of each hour (0-23). 1.0 = Average, 2.0 = Double Average.
        self.hourly_seasonality = {} 
        self.long_term_rate = 0.0    # 30-day baseline (tweets/hr)
        self.medium_term_rate = 0.0  # 72h baseline (tweets/hr)
        self.short_term_rate = 0.0   # 6h baseline (tweets/hr)
        
        self.daily_std_dev = 0.0

    def update_time(self):
        self.now_utc = datetime.now(timezone.utc)
        self.now_est = self.now_utc.astimezone(timezone(timedelta(hours=TIMEZONE_OFFSET)))
        self.now_myt = self.now_utc.astimezone(timezone(timedelta(hours=TIMEZONE_OFFSET_MYT)))

    def parse_iso_date(self, date_str):
        if not date_str: return None
        try:
            # Handle standard ISO format from API
            dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            return dt
        except Exception:
            try:
                # Fallback for milliseconds
                dt = datetime.strptime(date_str.split('.')[0], "%Y-%m-%dT%H:%M:%S")
                return dt.replace(tzinfo=timezone.utc)
            except:
                return None

    def parse_csv_date(self, date_str):
        """
        Intelligent date parser that avoids hardcoded years.
        Infers year based on current date if missing.
        """
        if not isinstance(date_str, str) or len(date_str) < 5: return None
        try:
            # Clean string
            clean_str = re.sub(r'\s+[A-Z]{3}\s*$', '', date_str).strip()
            
            # 1. Check if year exists in string
            if re.search(r'\d{4}', clean_str):
                # Try standard formats with Year
                formats = ['%Y-%m-%d %H:%M:%S', '%b %d, %Y, %I:%M:%S %p', '%Y-%m-%dT%H:%M:%S']
                for fmt in formats:
                    try:
                        dt = datetime.strptime(clean_str, fmt)
                        # Assume input is EST if naive, then convert to UTC
                        est_tz = timezone(timedelta(hours=TIMEZONE_OFFSET))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=est_tz)
                        return dt.astimezone(timezone.utc)
                    except: continue
            else:
                # 2. Handle "May 15, 10:00 PM" format (Missing Year)
                try:
                    dt_naive = datetime.strptime(clean_str, '%b %d, %I:%M:%S %p')
                    current_year = datetime.now().year
                    
                    # Try current year
                    dt = dt_naive.replace(year=current_year)
                    est_tz = timezone(timedelta(hours=TIMEZONE_OFFSET))
                    dt = dt.replace(tzinfo=est_tz)
                    
                    # Logic to handle year boundaries (e.g. Processing Dec data in Jan)
                    now_utc = datetime.now(timezone.utc)
                    diff_days = (dt.astimezone(timezone.utc) - now_utc).days
                    
                    if diff_days > 180: # Date is too far in future, must be previous year
                        dt = dt.replace(year=current_year - 1)
                    elif diff_days < -360: # Date is too far in past, might be next year (rare)
                        dt = dt.replace(year=current_year + 1)
                        
                    return dt.astimezone(timezone.utc)
                except: pass
            
            return None
        except Exception:
            return None

    def validate_market_integrity(self, title, api_start_dt):
        """Checks if market Title matches the API dates (Sanity Check)"""
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
                    candidate = api_start_dt.replace(year=base_year + y_offset, month=month_idx, day=day_idx).date()
                    candidates.append(abs((api_date - candidate).days))
                except ValueError: continue
            
            min_diff = min(candidates) if candidates else 0
            # If date in Title is > 5 days different from API start date, flag it
            if min_diff > 5: return False
            return True
        except Exception: return True

    def check_market_warning(self, title, api_start_dt):
        if not self.validate_market_integrity(title, api_start_dt):
            print(f"⚠️ Metadata Warning: '{title}' dates might be mismatched. Tracking anyway.")

    def fetch_and_archive_market(self, market_id, title, start_dt, end_dt):
        """Downloads historical data from closed markets to build better stats."""
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
                        # Interpolate tweets evenly across the hour
                        interval = 3600 / count
                        for i in range(count):
                            tweet_dt = base_dt + timedelta(seconds=i*interval)
                            syn_id = f"hist_{market_id}_{int(tweet_dt.timestamp())}_{i}"
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
                next(csv_reader, None) # Skip header
                for row in csv_reader:
                    if not row: continue
                    t_id = row[0]
                    t_date = None
                    # Try to find date column
                    for cell in row:
                        parsed = self.parse_csv_date(cell)
                        if parsed:
                            t_date = parsed
                            break
                    if t_id and t_date:
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

        # 2. Fetch Live Tweets (Recent)
        df_live = pd.DataFrame()
        try:
            # Fetch 2 days into future to catch edge cases, look back 60 days
            end_date_str = (self.now_utc + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            start_date_str = (self.now_utc - timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
            
            params = {'startDate': start_date_str, 'endDate': end_date_str}
            p_resp = requests.get(POSTS_API_URL, headers=headers, params=params, timeout=25)
            
            if p_resp.status_code == 200:
                posts_list = p_resp.json().get('data', {}).get('posts', [])
                if isinstance(p_resp.json().get('data'), list): posts_list = p_resp.json().get('data')
                
                live_rows = []
                for p in posts_list:
                    if p.get('id') and p.get('createdAt'):
                        dt = self.parse_iso_date(p['createdAt'])
                        if dt: 
                            live_rows.append({'id': str(p['id']).strip(), 'created_at': dt})
                df_live = pd.DataFrame(live_rows)
                if not df_live.empty: df_live.to_csv(self.live_csv, index=False)
            else:
                # Load cache
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

        # 5. SMART MERGE STRATEGY
        # Overlap prevention: Live > Old > Hist
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
            
            pre_len = len(full_df)
            full_df.drop_duplicates(subset=['id'], inplace=True)
            post_len = len(full_df)
            
            self.df = full_df
            # Create EST column for hour calculations
            self.df['est_time'] = self.df['created_at'].dt.tz_convert(timezone(timedelta(hours=TIMEZONE_OFFSET)))
            
            print(f"✅ Merged {post_len} tweets (Dropped {pre_len - post_len} duplicates).")
            self._calculate_stats()
        else:
            print("⚠️ No data available.")

    def _calculate_stats(self):
        if self.df is None or self.df.empty: return
        self.df['hour'] = self.df['est_time'].dt.hour
        self.df['dow'] = self.df['est_time'].dt.dayofweek
        
        # --- 1. Long Term Baseline (30 Days) ---
        start_30d = self.now_est - timedelta(days=30)
        df_30d = self.df[self.df['est_time'] >= start_30d].copy()
        
        if df_30d.empty: df_30d = self.df
        
        # Calculate Base Rate (Tweets/Hour) over 30 days
        hours_in_30d = (self.now_est - df_30d['est_time'].min()).total_seconds() / 3600
        self.long_term_rate = len(df_30d) / max(1, hours_in_30d)
        
        # --- 2. Seasonality (Hourly Heatmap) ---
        # Normalize so average hour = 1.0
        hourly_counts = df_30d.groupby('hour').size()
        total_counts = hourly_counts.sum()
        
        self.hourly_seasonality = {}
        for h in range(24):
            count = hourly_counts.get(h, 0)
            prob = count / max(1, total_counts)
            # Weight = Probability / (1/24)
            weight = prob / (1/24.0) 
            self.hourly_seasonality[h] = weight

        # --- 3. Velocity Vectors (Momentum) ---
        start_72h = self.now_est - timedelta(hours=72)
        start_6h = self.now_est - timedelta(hours=6)
        
        df_72h = self.df[self.df['est_time'] >= start_72h]
        df_6h = self.df[self.df['est_time'] >= start_6h]
        
        self.medium_term_rate = len(df_72h) / 72.0
        self.short_term_rate = len(df_6h) / 6.0
        
        # --- 4. Volatility (Std Dev of Daily Counts) ---
        daily_counts = self.df.groupby(self.df['est_time'].dt.date).size()
        self.daily_std_dev = daily_counts.tail(30).std()
        if pd.isna(self.daily_std_dev): self.daily_std_dev = 5.0

    def get_market_windows(self):
        return self.active_markets

    def get_market_bracket(self, val, duration_hours):
        v = int(val)
        if duration_hours > 480: # Monthly
            if v < 20: return "< 20", 0, 19
            if v >= 1400: return "1400+", 1400, 9999
            base = 600 if v >= 600 else 20
            step = 40 if v >= 600 else 20
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

    # --- ALGORITHM 1: LINEAR BASELINE ---
    def run_algo_v1(self):
        out = "=== ALGORITHM 1: LINEAR BASELINE ===\n"
        markets = self.get_market_windows()
        if not markets: return out + "No active markets found."
        
        for name, (start_utc, end_utc) in markets.items():
            mask = (self.df['created_at'] >= start_utc) & (self.df['created_at'] <= self.now_utc)
            current_total = len(self.df[mask])
            remaining_hours = max(0, (end_utc - self.now_utc).total_seconds() / 3600)
            total_duration = (end_utc - start_utc).total_seconds() / 3600
            
            # Use 72h rate (Medium Term)
            rate = self.medium_term_rate 
            final_pred = current_total + (rate * remaining_hours)
            
            r_str, _, _ = self.get_market_bracket(final_pred, total_duration)
            out += f"\n[{name}]\n"
            out += f"Current: {current_total} | Rem: {remaining_hours:.1f}h\n"
            out += f"Rate (72h): {rate:.2f}/hr -> Pred: {int(final_pred)} ({r_str})\n"
        return out

    # --- ALGORITHM 2: BURST MODE (CEILING) ---
    def run_algo_v2(self):
        """
        Assumes the current 'Short Term' mania (6h rate) continues unabated.
        Useful for calculating the upper bound of possibilities.
        """
        out = "=== ALGORITHM 2: BURST CEILING (MAX) ===\n"
        markets = self.get_market_windows()
        if not markets: return out + "Waiting..."

        burst_rate = max(self.short_term_rate, self.medium_term_rate)
        out += f"Burst Speed: {burst_rate:.2f}/hr (Assuming no sleep)\n"

        for name, (start_utc, end_utc) in markets.items():
            mask = (self.df['created_at'] >= start_utc) & (self.df['created_at'] <= self.now_utc)
            current_total = len(self.df[mask])
            
            sim_time = self.now_est
            end_est = end_utc.astimezone(timezone(timedelta(hours=TIMEZONE_OFFSET)))
            
            accumulated = 0
            while sim_time < end_est:
                # Apply Seasonality to the Burst Rate
                # Even in mania, he tweets less at 4 AM than 4 PM
                weight = self.hourly_seasonality.get(sim_time.hour, 1.0)
                expected = burst_rate * weight
                accumulated += expected
                sim_time += timedelta(hours=1)
            
            final_pred = current_total + accumulated
            r_str, _, _ = self.get_market_bracket(final_pred, (end_utc-start_utc).total_seconds()/3600)
            out += f"\n[{name}]\n"
            out += f"Max Ceiling: {int(final_pred)} ({r_str})\n"
        return out

    # --- ALGORITHM 3: MEAN REVERSION DECAY (REALISTIC) ---
    def run_algo_v3(self):
        """
        The Smart Algorithm.
        Blends Current Momentum -> Long Term Average over 48 hours.
        Captures the 'crash' after a tweet storm.
        """
        out = "=== ALGORITHM 3: MEAN REVERSION DECAY (SMART) ===\n"
        markets = self.get_market_windows()
        if not markets: return out + "Data insufficient."

        out += f"Long Term Base (30d): {self.long_term_rate:.2f}/hr\n"
        out += f"Current Speed (72h): {self.medium_term_rate:.2f}/hr\n"
        
        # Momentum Ratio: How manic is he?
        momentum_ratio = self.medium_term_rate / max(0.1, self.long_term_rate)
        
        for name, (start_utc, end_utc) in markets.items():
            mask = (self.df['created_at'] >= start_utc) & (self.df['created_at'] <= self.now_utc)
            current_total = len(self.df[mask])
            
            sim_time = self.now_est
            end_est = end_utc.astimezone(timezone(timedelta(hours=TIMEZONE_OFFSET)))
            
            accumulated_tweets = 0
            hours_simulated = 0
            
            # REVERSION SETTINGS
            # He usually maintains mania for 2-3 days max before crashing
            reversion_window_hours = 48.0 
            
            while sim_time < end_est:
                hours_simulated += 1
                
                # 1. Linear Decay Calculation
                decay_factor = min(1.0, hours_simulated / reversion_window_hours)
                
                # Blend: Start at Medium Term, slide to Long Term
                hourly_base = (self.medium_term_rate * (1 - decay_factor)) + (self.long_term_rate * decay_factor)
                
                # 2. Apply Hourly Seasonality (Sleep cycles)
                seasonality_multiplier = self.hourly_seasonality.get(sim_time.hour, 1.0)
                
                # 3. Add to total
                expected = hourly_base * seasonality_multiplier
                accumulated_tweets += expected
                
                sim_time += timedelta(hours=1)

            final_pred = current_total + accumulated_tweets
            
            # Confidence Interval Strategy
            days_rem = hours_simulated / 24
            # Widen margins if he is manic (High Momentum = High Volatility)
            vol_multiplier = 1.0 + (0.15 * max(0, momentum_ratio - 1.0))
            
            margin = (self.daily_std_dev * np.sqrt(max(days_rem, 0.5))) * vol_multiplier
            
            safe_min = final_pred - margin
            safe_max = final_pred + margin

            range_str, p_low, p_high = self.get_market_bracket(final_pred, (end_utc-start_utc).total_seconds()/3600)
            u_label, u_low, u_high = self.get_market_bracket(safe_max, (end_utc-start_utc).total_seconds()/3600)
            l_label, l_low, l_high = self.get_market_bracket(safe_min, (end_utc-start_utc).total_seconds()/3600)

            out += f"\n[{name}]\n"
            out += f"Projected: {int(final_pred)} (Range: {int(safe_min)}-{int(safe_max)})\n"
            out += f"PRIMARY BET: {range_str}\n"
            
            # Hedging Logic
            if u_low > p_high: out += f"HEDGE UP: {u_label}\n"
            if l_high < p_low: out += f"HEDGE DOWN: {l_label}\n"

        return out

    # --- ALGORITHM 4: ALPHA SCANNER ---
    def run_algo_v4(self):
        out = "=== ALGORITHM 4: MOMENTUM CHECK ===\n"
        ratio = self.medium_term_rate / max(0.1, self.long_term_rate)
        
        out += f"Momentum Ratio: {ratio:.2f}x\n"
        if ratio > 1.3: out += "⚠️ WARNING: Activity is Unsustainably High (Expect Reversion Down)\n"
        elif ratio < 0.7: out += "💤 NOTE: Activity is Low (Expect Reversion Up)\n"
        else: out += "✅ Activity is Normal.\n"
        
        return out
        
    # --- ALGORITHM 5: NEXT WEEK PRE-RUNNER ---
    def run_algo_v5(self):
        out = "=== ALGORITHM 5: NEXT WEEK PRE-RUNNER (ALPHA) ===\n"
        
        # 1. Project based on current 72h momentum (The "Hot" Hand)
        proj_momentum = self.medium_term_rate * 168
        
        # 2. Project based on 30d baseline (The "Safe" Hand)
        proj_baseline = self.long_term_rate * 168
        
        # 3. Determine target
        # If momentum is high, the market often underestimates the continuation
        target = (proj_momentum * 0.7) + (proj_baseline * 0.3) # Weighted blend
        
        range_str, low, high = self.get_market_bracket(target, 168)
        
        out += f"Current 72h Trend Projects: {int(proj_momentum)} tweets/week\n"
        out += f"30d Baseline Projects: {int(proj_baseline)} tweets/week\n"
        out += f"🎯 FAIR VALUE TARGET: {int(target)} ({range_str})\n"
        
        if proj_momentum > proj_baseline * 1.2:
            out += "Strategy: MOMENTUM PLAY. Buy brackets slightly above current average.\n"
        elif proj_momentum < proj_baseline * 0.8:
            out += "Strategy: REVERSION PLAY. Buy brackets lower than current perception.\n"
        else:
            out += "Strategy: NEUTRAL. Accumulate middle brackets.\n"
            
        return out

# --- DISCORD CLIENT ---
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
        if not channel: 
            print("Channel not found. Check CHANNEL_ID.")
            return
            
        print("Running Analysis Cycle...")
        try: await channel.purge(limit=10) # Clean up previous messages
        except Exception: pass
        
        self.engine.update_time()
        self.engine.refresh_data()
        
        if self.engine.df is None or self.engine.df.empty: return

        report_v1 = self.engine.run_algo_v1()
        report_v2 = self.engine.run_algo_v2()
        report_v3 = self.engine.run_algo_v3()
        report_v4 = self.engine.run_algo_v4()
        report_v5 = self.engine.run_algo_v5()
        
        ts_est = self.engine.now_est.strftime('%Y-%m-%d %H:%M EST')
        
        try:
            await channel.send(f"**🚀 Elon Prediction Update - {ts_est}**")
            await channel.send(f"```yaml\n{report_v1}\n```")
            await channel.send(f"```yaml\n{report_v2}\n```")
            await channel.send(f"```yaml\n{report_v3}\n```")
            await channel.send(f"```yaml\n{report_v4}\n```")
            await channel.send(f"```yaml\n{report_v5}\n```")
            print("Reports sent.")
        except Exception as e: print(f"Error sending: {e}")

    @monitor_task.before_loop
    async def before_monitor(self):
        await self.wait_until_ready()

if __name__ == "__main__":
    intents = discord.Intents.default()
    client = ElonPredictorBot(intents=intents)
    client.run(DISCORD_TOKEN)
