import requests
import time
import json
import os
import sys
from datetime import datetime, time as dtime, timedelta, date
import warnings

# 尝试导入中国节假日库
try:
    from chinese_calendar import is_holiday, is_workday
    HAS_CALENDAR = True
except ImportError:
    HAS_CALENDAR = False
    print("[System] 未检测到 chinese_calendar 库，将仅依据周末判断休市。")

warnings.filterwarnings('ignore')

# ==========================================
# 1. 智能配置管理
# ==========================================
class ConfigManager:
    def __init__(self, file_path='/Volumes/T7/VSCode/AlphaHunter/Portfolio/portfolio.json'):
        self.file_path = file_path
        self.last_mtime = 0
        self.config = None 

    def check_and_reload(self):
        try:
            if not os.path.exists(self.file_path):
                print(f"\r[Error] 找不到配置文件: {self.file_path} (保持旧配置运行)", end="")
                return False, self.config

            current_mtime = os.path.getmtime(self.file_path)
            if current_mtime == self.last_mtime and self.config is not None:
                return False, self.config

            with open(self.file_path, 'r', encoding='utf-8') as f:
                new_config = json.load(f)
            
            if "token" not in new_config or "portfolio" not in new_config:
                return False, self.config

            self.config = new_config
            self.last_mtime = current_mtime
            
            stock_names = [s['name'] for s in self.config['portfolio']]
            # 使用 \r 清除之前的等待日志，保持界面整洁
            print(f"\n[System] 配置热重载成功! 监控: {stock_names}")
            
            return True, self.config

        except Exception as e:
            print(f"\n[Config] 读取异常: {e}")
            return False, self.config

# ==========================================
# 2. 交易日历模块 (逻辑增强)
# ==========================================
class MarketCalendar:
    @staticmethod
    def is_trading_day(dt_date):
        """判断某一天是否是交易日"""
        if dt_date.weekday() >= 5: return False
        if HAS_CALENDAR:
            if is_holiday(dt_date): return False
            if is_workday(dt_date) and dt_date.weekday() >= 5: return False
        return True

    @staticmethod
    def get_next_market_open_time():
        """
        计算下一个开启监控的时间点。
        返回: datetime 对象
        """
        now = datetime.now()
        
        # 场景 A: 今天是交易日，且还没到下午收盘 (午休也算在内，因为要等下午开盘)
        if MarketCalendar.is_trading_day(now.date()):
            # 1. 如果还没到早上开盘 ( < 09:15 ) -> 目标是今天 09:15
            if now.time() < dtime(9, 15):
                return datetime.combine(now.date(), dtime(9, 15))
            
            # 2. 如果是午休时间 ( 11:35 - 12:55 ) -> 目标是今天 12:55
            if dtime(11, 35) < now.time() < dtime(12, 55):
                return datetime.combine(now.date(), dtime(12, 55))
            
            # 3. 如果还在交易时间段内 (09:15-11:35 或 12:55-15:05) -> 立即返回当前时间 (无需等待)
            # 注意：这里稍微放宽一点范围，防止临界点卡死
            if now.time() <= dtime(15, 5):
                return now 

        # 场景 B: 今天已收盘 或 今天非交易日 -> 找下一个交易日的 09:15
        target_date = now.date() + timedelta(days=1)
        while not MarketCalendar.is_trading_day(target_date):
            target_date += timedelta(days=1)
            
        return datetime.combine(target_date, dtime(9, 15))

# ==========================================
# 3. 推送服务
# ==========================================
class Pusher:
    def __init__(self, token):
        self.token = token
        self.url = "http://www.pushplus.plus/send"

    def update_token(self, new_token):
        self.token = new_token

    def send(self, title, content):
        if not self.token: return
        data = {"token": self.token, "title": title, "content": content, "template": "markdown"}
        try:
            requests.post(self.url, json=data, timeout=10)
        except Exception:
            pass

# ==========================================
# 4. 数据引擎 (双接口+强伪装版)
# ==========================================
class EastMoneyEngine:
    def __init__(self):
        # 模拟真实的浏览器请求头
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "http://quote.eastmoney.com/center/gridlist.html",
            "Host": "push2.eastmoney.com",
            "Connection": "keep-alive"
        }

    def _get_secid(self, symbol):
        symbol = str(symbol).strip()
        # 沪市: 6/5/9/11开头 -> 1
        if symbol.startswith(('6', '5', '9', '11')):
            return f"1.{symbol}"
        # 深市/北交: 其他 -> 0
        return f"0.{symbol}"

    def _request_batch(self, secids_str):
        """接口A: 批量列表接口"""
        url = "https://push2.eastmoney.com/api/qt/ulist/get"
        params = {
            "invt": "2", "fltt": "2", "fields": "f12,f2,f3,f10", 
            "secids": secids_str
        }
        try:
            resp = requests.get(url, params=params, headers=self.headers, timeout=5)
            return resp.json()
        except Exception:
            return None

    def _request_single(self, secid):
        """接口B: 个股详情接口 (备用，更稳定)"""
        url = "https://push2.eastmoney.com/api/qt/stock/get"
        # f43:现价, f170:涨跌幅, f168:量比 (注意字段编号变化)
        params = {
            "invt": "2", "fltt": "2", "fields": "f57,f43,f170,f168",
            "secid": secid
        }
        try:
            resp = requests.get(url, params=params, headers=self.headers, timeout=5)
            j = resp.json()
            if j and j.get('data'):
                d = j['data']
                # 统一格式转换
                return {
                    'f12': d.get('f57'), # 代码
                    'f2': d.get('f43'),  # 现价
                    'f3': d.get('f170'), # 涨跌幅
                    'f10': d.get('f168') # 量比
                }
            return None
        except Exception:
            return None

    def fetch(self, portfolio):
        if not portfolio: return [], 0, 0
        
        port_map = {str(p['symbol']).strip(): p for p in portfolio}
        secid_list = [self._get_secid(sym) for sym in port_map.keys()]
        
        # 1. 优先尝试批量请求
        json_data = self._request_batch(",".join(secid_list))
        
        valid_data = []
        is_batch_success = False

        if json_data and json_data.get('data') and json_data['data'].get('diff'):
            valid_data = json_data['data']['diff']
            is_batch_success = True
        
        # 2. 如果批量失败，启动备用方案 (逐个请求)
        if not is_batch_success:
            print(f"\r[Warn] 批量接口受阻，切换单点突破模式...", end="", flush=True)
            for secid in secid_list:
                single_data = self._request_single(secid)
                if single_data:
                    valid_data.append(single_data)
                else:
                    # 只有单点也失败了，才是真的代码错了
                    raw_code = secid.split('.')[1]
                    name = port_map.get(raw_code, {}).get('name', '未知')
                    print(f"\n   ❌ 无法获取: {name} ({secid})")

        # 3. 数据清洗
        results = []
        tp, tmv = 0, 0
        
        for item in valid_data:
            symbol = str(item['f12'])
            if symbol not in port_map: continue
            
            # 价格清洗
            try:
                price = float(item['f2'])
                if price == 0: continue # 停牌或无效
            except (ValueError, TypeError):
                continue

            # 涨跌幅清洗
            try:
                change = float(item['f3'])
            except (ValueError, TypeError):
                change = 0.0

            # 量比清洗
            try:
                vol_ratio = float(item['f10'])
            except (ValueError, TypeError):
                vol_ratio = 0.0
            
            cfg = port_map[symbol]
            profit = (price - cfg['cost']) * cfg['vol']
            profit_pct = (price - cfg['cost']) / cfg['cost'] * 100 if cfg['cost'] != 0 else 0
            mv = price * cfg['vol']
            
            tp += profit; tmv += mv
            results.append({
                "name": cfg['name'], "symbol": symbol, "price": price,
                "change": change, "vol_ratio": vol_ratio,
                "profit": profit, "profit_pct": profit_pct, "cost": cfg['cost']
            })
            
        return results, tp, tmv
        
# ==========================================
# 5. 核心监控逻辑 (启动即反馈版)
# ==========================================

class Monitor:
    def __init__(self):
        # 请确认路径是否正确
        self.cfg_mgr = ConfigManager(file_path="/Volumes/T7/VSCode/AlphaHunter/Portfolio/portfolio.json")
        
        updated, cfg = self.cfg_mgr.check_and_reload()
        if not cfg:
            print("❌ 启动失败：请检查 portfolio.json")
            sys.exit(1)
            
        self.pusher = Pusher(cfg['token'])
        self.engine = EastMoneyEngine()
        self.alert_cooldown = {} 
        self.last_report_minute = ""

    def generate_report(self, data, tp, tmv):
        color = "#ff0000" if tp > 0 else "#008000"
        sign = "+" if tp > 0 else ""
        md = f"#### 💰 账户动态\n**市值**: {tmv:,.0f} | **盈亏**: <font color='{color}'>{sign}{tp:,.0f}</font>\n\n"
        md += "| 名称 | 现价 | 涨跌 | 量比 | 盈亏 |\n|---|---|---|---|---|\n"
        for i in data:
            c_c = "#ff0000" if i['change']>0 else "#008000"
            p_c = "#ff0000" if i['profit']>0 else "#008000"
            md += f"| {i['name']} | {i['price']} | <font color='{c_c}'>{i['change']}%</font> | {i['vol_ratio']} | <font color='{p_c}'>{i['profit']:.0f}</font> |\n"
        return md

    def check_alerts(self, data, alert_cfg):
        alerts = []
        now_ts = time.time()
        for i in data:
            sym = i['symbol']
            if sym in self.alert_cooldown and now_ts - self.alert_cooldown[sym] < 1800: continue
            
            triggers = []
            if abs(i['change']) >= alert_cfg['alert_change']: triggers.append(f"波 {i['change']}%")
            if i['vol_ratio'] >= alert_cfg['alert_vol_ratio']: triggers.append(f"量 {i['vol_ratio']}")
            if i['profit_pct'] <= alert_cfg['stop_loss']: triggers.append(f"损 {i['profit_pct']:.1f}%")
            if i['profit_pct'] >= alert_cfg['take_profit']: triggers.append(f"盈 {i['profit_pct']:.1f}%")
            
            if triggers:
                sign = "+" if i['profit']>0 else ""
                alerts.append(f"**{i['name']}**: {' '.join(triggers)}\n现价:{i['price']} 盈亏:{sign}{i['profit']:.0f}")
                self.alert_cooldown[sym] = now_ts
        if alerts: self.pusher.send("🚨 异动警报", "\n---\n".join(alerts))

    def run_once_check(self):
        """执行一次强制检查（用于启动自检）"""
        print("[Init] 正在执行启动自检...", end="", flush=True)
        updated, cfg = self.cfg_mgr.check_and_reload()
        if cfg:
            self.pusher.update_token(cfg['token'])
            data, tp, tmv = self.engine.fetch(cfg['portfolio'])
            if data:
                self.pusher.send("🚀 系统上线 (启动自检)", self.generate_report(data, tp, tmv))
                print(" -> 自检消息已发送 ✅")
            else:
                print(" -> 获取数据失败 ❌")
        else:
            print(" -> 配置加载失败 ❌")

    def start(self):
        print(f"[System] 监控服务启动，进程ID: {os.getpid()}")

        # ==========================================
        # 🟢 核心修改：在进入死循环前，先强制运行一次
        # 这样无论现在是几点，你都能立马收到消息
        # ==========================================
        self.run_once_check()

        print("[System] 进入自动监控循环...")

        while True:
            try:
                # 1. 热重载配置
                updated, cfg = self.cfg_mgr.check_and_reload()
                if updated: self.pusher.update_token(cfg['token'])

                # 2. 智能等待逻辑
                target_dt = MarketCalendar.get_next_market_open_time()
                now = datetime.now()
                
                # 如果现在是休市时间（包括午休）
                if target_dt > now + timedelta(seconds=5):
                    time_diff = target_dt - now
                    hours = int(time_diff.total_seconds() // 3600)
                    minutes = int((time_diff.total_seconds() % 3600) // 60)
                    print(f"\r[Sleep] 休市中。将在 {target_dt.strftime('%H:%M')} 唤醒 (剩余 {hours}小时{minutes}分)...", end="", flush=True)
                    
                    while datetime.now() < target_dt:
                        self.cfg_mgr.check_and_reload()
                        time.sleep(60) 
                    continue 

                # 3. 执行监控
                data, tp, tmv = self.engine.fetch(cfg['portfolio'])
                
                if not data: 
                    print(f"\r[Retry] 数据空，重试...", end="", flush=True)
                    time.sleep(5); continue

                print(f"\r[Run] 监控中... 总盈亏: {tp:.0f}      ", end="", flush=True)

                self.check_alerts(data, cfg['alert_config'])

                t_str = now.strftime("%H:%M")
                report_times = ["09:30", "10:00", "11:00", "13:00", "14:00", "15:00"]
                if t_str in report_times and t_str != self.last_report_minute:
                    titles = {"09:30": "🚀 开盘", "15:00": "🌙 收盘"}
                    title = titles.get(t_str, f"⏰ {now.hour}点播报")
                    self.pusher.send(title, self.generate_report(data, tp, tmv))
                    self.last_report_minute = t_str

                time.sleep(30)

            except KeyboardInterrupt:
                print("\n停止监控"); break
            except Exception as e:
                print(f"\n[Error] {e}"); time.sleep(30)
if __name__ == "__main__":
    Monitor().start()
    #  nohup python3 -u /Volumes/T7/VSCode/AlphaHunter/Monitor/stock_monitor.py > /Volumes/T7/VSCode/AlphaHunter/Monitor/log.txt 2>&1 &
 
    #  ps -ef | grep stock_monitor.py
    #     (base) macbook@ZhonghuadeMac-mini:/Volumes/T7/VSCode/AlphaHunter$ ps -ef | grep stock_monitor.py
    #   501 35754 34049   0  1:58下午 ttys009    0:00.42 python3 /Volumes/T7/VSCode/AlphaHunter/stock_monitor.py
    #   501 36085 34049   0  2:07下午 ttys009    0:00.00 grep stock_monitor.py

    #  kill 35754
    # pkill -f stock_monitor.py