import requests
import time
from datetime import datetime, time as dtime
import warnings
import sys
import json
import traceback

# 忽略警告
warnings.filterwarnings('ignore')

# ==========================================
# 1. 用户配置区域 (已根据截图更新)
# ==========================================
CONFIG = {
    # PUSHPLUS 令牌
    "TOKEN": "19995f2a28a4448aa9fc7bd53c137211",
    
    # 持仓列表：[代码, 名称, 成本价, 持仓数量]
    "PORTFOLIO": [
        {"symbol": "002415", "name": "海康威视", "cost": 29.790, "vol": 100},
        {"symbol": "000921", "name": "海信家电", "cost": 24.820, "vol": 100},
        {"symbol": "600104", "name": "上汽集团", "cost": 15.370, "vol": 100},
        {"symbol": "600886", "name": "国投电力", "cost": 13.350, "vol": 100},
        {"symbol": "600919", "name": "江苏银行", "cost": 10.440, "vol": 100},
        {"symbol": "603565", "name": "中谷物流", "cost": 10.080, "vol": 100},
        {"symbol": "601988", "name": "中国银行", "cost": 5.736,  "vol": 200},
        {"symbol": "600027", "name": "华电国际", "cost": 5.055,  "vol": 200},
        {"symbol": "002948", "name": "青岛银行", "cost": 4.445,  "vol": 200},
    ],

    # 预警阈值
    "ALERT_CHANGE": 5.0,    # 涨跌幅超过 ±5% 报警
    "ALERT_VOL_RATIO": 3.0, # 量比超过 3.0 报警 (主力异动)
    "STOP_LOSS": -5.0,      # 相对成本亏损 5% 报警
    "TAKE_PROFIT": 10.0     # 相对成本盈利 10% 报警
}

# ==========================================
# 2. 推送服务模块
# ==========================================
class Pusher:
    def __init__(self, token):
        self.token = token
        self.url = "http://www.pushplus.plus/send"

    def send(self, title, content, template='markdown'):
        data = {
            "token": self.token,
            "title": title,
            "content": content,
            "template": template
        }
        try:
            resp = requests.post(self.url, json=data, timeout=10)
            if resp.status_code != 200:
                print(f"[Error] 推送失败: {resp.text}")
            return True
        except Exception as e:
            print(f"[Error] 推送网络异常: {e}")
            return False

# ==========================================
# 3. 数据引擎 (东方财富 HTTPS 版)
# ==========================================
class EastMoneyEngine:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Referer": "https://quote.eastmoney.com/"
        }
    
    def _get_secid(self, symbol):
        # 沪市(6开头)用1.xxx，深市(0/3开头)用0.xxx
        if symbol.startswith('6'):
            return f"1.{symbol}"
        else:
            return f"0.{symbol}"

    def fetch_realtime_data(self, portfolio):
        try:
            secids = ",".join([self._get_secid(p['symbol']) for p in portfolio])
            
            url = "https://push2.eastmoney.com/api/qt/ulist/get"
            params = {
                "invt": "2",
                "fltt": "2",
                "fields": "f12,f14,f2,f3,f10", # 代码,名称,现价,涨跌幅,量比
                "secids": secids,
                "pn": "1",
                "np": "1"
            }

            resp = requests.get(url, headers=self.headers, params=params, timeout=5)
            
            if resp.status_code != 200:
                print(f"[Warn] 接口状态码: {resp.status_code}")
                return [], 0, 0

            data_json = resp.json()
            
            if not data_json or 'data' not in data_json or data_json['data'] is None:
                return [], 0, 0

            diff_list = data_json['data']['diff']
            
            results = []
            total_profit = 0
            total_market_value = 0
            port_map = {p['symbol']: p for p in portfolio}

            for item in diff_list:
                symbol = item['f12']
                if symbol not in port_map: continue
                
                # 处理停牌或无效数据
                if item['f2'] == '-': 
                    continue

                current_price = float(item['f2'])
                change_pct = float(item['f3'])
                vol_ratio = float(item['f10']) if item['f10'] != '-' else 0.0

                if current_price == 0: continue

                stock_conf = port_map[symbol]
                cost = stock_conf['cost']
                vol = stock_conf['vol']
                
                market_val = current_price * vol
                profit = market_val - (cost * vol)
                profit_pct = (current_price - cost) / cost * 100
                
                total_profit += profit
                total_market_value += market_val
                
                results.append({
                    "name": stock_conf['name'],
                    "symbol": symbol,
                    "price": current_price,
                    "change": change_pct,
                    "vol_ratio": vol_ratio,
                    "profit": profit,
                    "profit_pct": profit_pct,
                    "cost": cost
                })
                
            return results, total_profit, total_market_value

        except Exception as e:
            print(f"[Error] 数据获取异常: {e}")
            return [], 0, 0

# ==========================================
# 4. 监控逻辑
# ==========================================
class Monitor:
    def __init__(self):
        self.pusher = Pusher(CONFIG["TOKEN"])
        self.engine = EastMoneyEngine()
        self.portfolio = CONFIG["PORTFOLIO"]
        self.last_push_hour = -1 
        self.alert_cooldown = {} # 报警冷却

    def generate_report(self, data, total_p, total_mv):
        color = "#ff0000" if total_p > 0 else "#008000"
        sign = "+" if total_p > 0 else ""
        
        md = f"#### 💰 账户动态\n"
        md += f"**总市值**: ¥{total_mv:,.0f}\n"
        md += f"**总盈亏**: <font color='{color}'>{sign}{total_p:,.0f} 元</font>\n\n"
        md += "| 名称 | 现价 | 涨跌 | 量比 | 盈亏 |\n"
        md += "|---|---|---|---|---|\n"
        
        for item in data:
            c_color = "#ff0000" if item['change'] > 0 else "#008000"
            p_color = "#ff0000" if item['profit'] > 0 else "#008000"
            md += f"| {item['name']} | {item['price']} | <font color='{c_color}'>{item['change']}%</font> | {item['vol_ratio']} | <font color='{p_color}'>{item['profit']:.0f}</font> |\n"
        return md

    def check_alerts(self, data):
        alerts = []
        current_ts = time.time()
        for item in data:
            symbol = item['symbol']
            
            # 冷却机制：30分钟内不重复报同一只股
            if symbol in self.alert_cooldown and current_ts - self.alert_cooldown[symbol] < 1800: 
                continue
            
            triggers = []
            if abs(item['change']) >= CONFIG["ALERT_CHANGE"]: triggers.append(f"股价波动 {item['change']}%")
            if item['vol_ratio'] >= CONFIG["ALERT_VOL_RATIO"]: triggers.append(f"量比突增 {item['vol_ratio']}")
            if item['profit_pct'] <= CONFIG["STOP_LOSS"]: triggers.append(f"止损预警 {item['profit_pct']:.1f}%")
            if item['profit_pct'] >= CONFIG["TAKE_PROFIT"]: triggers.append(f"止盈提醒 {item['profit_pct']:.1f}%")
            
            if triggers:
                sign = "+" if item['profit'] > 0 else ""
                msg = f"### ⚠️ {item['name']} 异动\n"
                msg += f"**原因**: {' | '.join(triggers)}\n"
                msg += f"---\n"
                msg += f"现价: {item['price']} (成本: {item['cost']})\n"
                msg += f"量比: {item['vol_ratio']}\n"
                msg += f"盈亏: {sign}{item['profit']:.0f}元 ({item['profit_pct']:.1f}%)\n"
                alerts.append(msg)
                self.alert_cooldown[symbol] = current_ts
        
        if alerts: 
            self.pusher.send(title="🚨 持仓紧急预警", content="\n".join(alerts))

    def run(self):
        print(f"[System] 监控服务启动...")
        print(f"[System] 持仓数: {len(self.portfolio)}")
        self.pusher.send("🤖 系统上线", f"监控已启动\n持仓数: {len(self.portfolio)}\n\n(该消息证明服务正常)")
        
        while True:
            try:
                now = datetime.now()
                # 交易时间: 09:15-11:35, 12:55-15:05
                is_trading = (dtime(9, 15) <= now.time() <= dtime(11, 35)) or (dtime(12, 55) <= now.time() <= dtime(15, 5))
                
                if not is_trading:
                    print(f"\r[Sleep] 休市中... {now.strftime('%H:%M:%S')}", end="")
                    time.sleep(60)
                    continue

                data, tp, tmv = self.engine.fetch_realtime_data(self.portfolio)
                if not data:
                    time.sleep(10)
                    continue
                
                print(f"\r[Run] 监控中... 总盈亏: {tp:.0f}    ", end="")
                
                # 1. 检查异动
                self.check_alerts(data)
                
                # 2. 定时播报逻辑（开盘、整点、收盘）
                current_time_str = now.strftime("%H:%M")
                
                # 定义需要播报的时间点
                # 9:30(开盘), 10:00, 11:00, 13:00(午后开盘), 14:00, 15:00(收盘)
                report_times = ["09:30", "10:00", "11:00", "13:00", "14:00", "15:00"]
                
                # 检查当前分钟是否在预设时间内，且这一分钟还没推送过
                if current_time_str in report_times and current_time_str != getattr(self, 'last_report_minute', ''):
                    
                    if current_time_str == "09:30":
                        title = "🚀 早盘开盘播报"
                    elif current_time_str == "15:00":
                        title = "🔔 收盘总结报表"
                    else:
                        title = f"⏰ {now.hour}点整点播报"
                    
                    # 发送推送
                    self.pusher.send(title, self.generate_report(data, tp, tmv))
                    
                    # 记录这一分钟已经推过了，防止30秒轮询导致一分钟内推两次
                    self.last_report_minute = current_time_str
                
                time.sleep(30) # 30秒轮询一次
                
            except KeyboardInterrupt:
                print("\n[Stop] 用户停止监控")
                break
            except Exception as e:
                print(f"\n[Error] 主循环报错: {e}")
                time.sleep(30)

# ==========================================
# 5. 主程序入口
# ==========================================
if __name__ == "__main__":
    # 默认直接启动监控
    # 如果想测试，可以临时加一行 monitor.engine.fetch_realtime_data(...)
    monitor = Monitor()
    monitor.run()

    
    #  nohup python3 /Volumes/T7/VSCode/AlphaHunter/stock_monitor.py > /Volumes/T7/VSCode/AlphaHunter/log.txt 2>&1 &

    #  ps -ef | grep stock_monitor.py
    #     (base) macbook@ZhonghuadeMac-mini:/Volumes/T7/VSCode/AlphaHunter$ ps -ef | grep stock_monitor.py
    #   501 35754 34049   0  1:58下午 ttys009    0:00.42 python3 /Volumes/T7/VSCode/AlphaHunter/stock_monitor.py
    #   501 36085 34049   0  2:07下午 ttys009    0:00.00 grep stock_monitor.py

    #  kill 35754

