import requests
import time
import json
import os
import sys
from datetime import datetime, time as dtime, timedelta, date
import warnings
from chinese_calendar import is_holiday, is_workday
HAS_CALENDAR = True

warnings.filterwarnings('ignore')

# ==========================================
# 1. 配置管理模块 (支持热更新)
# ==========================================
class ConfigManager:
    def __init__(self, file_path='portfolio.json'):
        self.file_path = file_path
        self.last_mtime = 0
        self.config = {}

    def load(self):
        """加载配置文件，支持热更新"""
        try:
            if not os.path.exists(self.file_path):
                print(f"[Error] 找不到配置文件: {self.file_path}")
                return None

            # 检查文件修改时间，没变就不读取IO
            current_mtime = os.path.getmtime(self.file_path)
            # 强制每分钟至少重读一次，或者文件变动时重读
            if current_mtime != self.last_mtime or not self.config:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    self.config = json.load(f)
                    self.last_mtime = current_mtime
                # print(f"[System] 配置已更新/加载") # 调试用，生产环境可注释
            return self.config
        except Exception as e:
            print(f"[Error] 配置文件读取失败: {e}")
            return self.config # 返回旧配置防止崩溃

# ==========================================
# 2. 交易日历模块
# ==========================================
class MarketCalendar:
    @staticmethod
    def is_trading_day():
        """
        判断今天是否为A股交易日
        逻辑：
        1. 周末(周六周日) -> 休市
        2. 法定节假日 -> 休市
        3. 调休的周六日 -> A股通常依然休市 (与正常工作日不同)
        """
        today = date.today()
        
        # 1. 基础判断：如果是周六周日
        if today.weekday() >= 5:
            return False

        # 2. 节假日库判断
        if HAS_CALENDAR:
            # is_holiday 返回 True 表示是假期(含周末)
            # is_workday 返回 True 表示是工作日(含调休)
            
            # 这里的逻辑比较绕：A股不仅节假日不开，调休上班的周末也不开
            # 所以逻辑是：必须是法定工作日，且不能是周末
            if is_holiday(today):
                return False
            
            # 如果是调休上班的周末（is_workday是True，但weekday是5或6），股市是不开的
            if is_workday(today) and today.weekday() >= 5:
                return False
                
        return True

    @staticmethod
    def get_seconds_until_market_open():
        """计算距离下一个交易日开盘(9:15)还有多少秒"""
        now = datetime.now()
        target_date = now.date()
        
        # 寻找下一个交易日
        while True:
            # 如果是今天，但已经过了收盘时间(15:30后算过)，则算明天
            if target_date == now.date() and now.time() > dtime(15, 30):
                target_date += timedelta(days=1)
                continue
            
            # 检查 target_date 是否是交易日
            # 这里简化逻辑：如果是周末就跳过，如果是今天且没过收盘则检查是否交易日
            is_trade = True
            if target_date.weekday() >= 5: is_trade = False
            if HAS_CALENDAR and is_holiday(target_date): is_trade = False
            
            if is_trade:
                break
            target_date += timedelta(days=1)
        
        target_time = datetime.combine(target_date, dtime(9, 15))
        delta = (target_time - now).total_seconds()
        return max(60, delta) # 至少休眠60秒

# ==========================================
# 3. 推送服务
# ==========================================
class Pusher:
    def __init__(self, config_manager):
        self.cfg_mgr = config_manager
        self.url = "http://www.pushplus.plus/send"

    def send(self, title, content):
        cfg = self.cfg_mgr.load()
        if not cfg: return
        
        data = {
            "token": cfg['token'],
            "title": title,
            "content": content,
            "template": "markdown"
        }
        try:
            requests.post(self.url, json=data, timeout=10)
        except Exception as e:
            print(f"[Error] 推送失败: {e}")

# ==========================================
# 4. 数据引擎 (EastMoney)
# ==========================================
class EastMoneyEngine:
    def fetch(self, portfolio):
        if not portfolio: return [], 0, 0
        try:
            # 构造 secids
            secids = []
            for p in portfolio:
                prefix = "1" if p['symbol'].startswith('6') else "0"
                secids.append(f"{prefix}.{p['symbol']}")
            
            url = "https://push2.eastmoney.com/api/qt/ulist/get"
            params = {
                "invt": "2", "fltt": "2", "fields": "f12,f2,f3,f10", 
                "secids": ",".join(secids)
            }
            
            resp = requests.get(url, params=params, timeout=5, headers={"Referer": "https://eastmoney.com"})
            data = resp.json().get('data', {}).get('diff', [])
            
            results = []
            total_profit = 0
            total_mv = 0
            port_map = {p['symbol']: p for p in portfolio}

            for item in data:
                symbol = item['f12']
                if symbol not in port_map or item['f2'] == '-': continue
                
                price = float(item['f2'])
                change = float(item['f3'])
                vol_ratio = float(item['f10']) if item['f10'] != '-' else 0.0
                
                cfg = port_map[symbol]
                profit = (price - cfg['cost']) * cfg['vol']
                profit_pct = (price - cfg['cost']) / cfg['cost'] * 100
                mv = price * cfg['vol']
                
                total_profit += profit
                total_mv += mv
                
                results.append({
                    "name": cfg['name'], "symbol": symbol, "price": price,
                    "change": change, "vol_ratio": vol_ratio,
                    "profit": profit, "profit_pct": profit_pct, "cost": cfg['cost']
                })
            return results, total_profit, total_mv
        except Exception:
            return [], 0, 0

# ==========================================
# 5. 核心监控逻辑
# ==========================================
class Monitor:
    def __init__(self):
        self.config_mgr = ConfigManager(file_path="/Volumes/T7/VSCode/AlphaHunter/Portfolio/portfolio.json")
        self.pusher = Pusher(self.config_mgr)
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
            # 冷却30分钟
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

    def start(self):
        print("[System] 监控服务启动")
        # 启动时先加载一次配置测试
        cfg = self.config_mgr.load()
        if not cfg: 
            print("配置文件错误，退出"); return
        
        self.pusher.send("🤖 系统上线", f"监控已启动，当前持仓: {len(cfg['portfolio'])}只")

        while True:
            try:
                # 1. 检查是否是交易日
                if not MarketCalendar.is_trading_day():
                    sleep_sec = MarketCalendar.get_seconds_until_market_open()
                    hours = sleep_sec / 3600
                    print(f"\r[Sleep] 非交易日/休市。休眠 {hours:.1f} 小时...", end="", flush=True)
                    # 避免系统时间跳变或长时间sleep不可中断，分段sleep
                    time.sleep(min(sleep_sec, 3600)) 
                    continue

                now = datetime.now()
                # 2. 检查交易时段 (9:15 - 15:05, 包含集合竞价和稍微延后)
                is_trading = (dtime(9, 15) <= now.time() <= dtime(11, 35)) or \
                             (dtime(12, 55) <= now.time() <= dtime(15, 5))
                
                if not is_trading:
                    print(f"\r[Wait] 等待开盘... {now.strftime('%H:%M:%S')}", end="", flush=True)
                    time.sleep(60)
                    continue

                # 3. 加载最新配置 (实现热更新)
                cfg = self.config_mgr.load()
                
                # 4. 获取数据
                data, tp, tmv = self.engine.fetch(cfg['portfolio'])
                if not data: 
                    time.sleep(10); continue

                print(f"\r[Run] 监控中... 总盈亏: {tp:.0f}      ", end="", flush=True)

                # 5. 异动检查
                self.check_alerts(data, cfg['alert_config'])

                # 6. 定时推送 (开盘、整点、收盘)
                t_str = now.strftime("%H:%M")
                report_times = ["09:30", "10:00", "11:30", "13:20", "14:40", "15:00"]
                
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