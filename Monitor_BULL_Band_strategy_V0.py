import requests
import pandas as pd
import akshare as ak
import numpy as np
import time
import json
import os
import warnings
from datetime import datetime, timedelta, time as dtime

warnings.filterwarnings('ignore')

# ==========================================
# 1. 配置与状态管理
# ==========================================
CONFIG = {
    "TOKEN": "19995f2a28a4448aa9fc7bd53c137211", # 你的 PushPlus Token
    "STATE_FILE": "portfolio_state.json", # 用于保存持仓状态的文件
    # 如果是第一次运行，这里定义初始关注列表
    # stage: 0=空仓, 1=底仓, 2=补仓1, 3=满仓
    "WATCH_LIST": [
        {"symbol": "002415", "name": "海康威视", "stage": 1, "cost": 29.79, "shares": 100, "max_profit": 0.0},
        {"symbol": "600519", "name": "贵州茅台", "stage": 0, "cost": 0.0,   "shares": 0,   "max_profit": 0.0},
        {"symbol": "300059", "name": "东方财富", "stage": 0, "cost": 0.0,   "shares": 0,   "max_profit": 0.0},
    ]
}

class StateManager:
    """管理持仓状态，确保程序重启后记得之前的买卖进度"""
    def __init__(self, file_path):
        self.file_path = file_path
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        # 如果文件不存在，初始化为 CONFIG 中的列表
        initial_dict = {item['symbol']: item for item in CONFIG['WATCH_LIST']}
        return initial_dict

    def save(self):
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=4, ensure_ascii=False)
            
    def get_stock(self, symbol):
        return self.data.get(symbol, None)

    def update_stock(self, symbol, **kwargs):
        if symbol in self.data:
            for k, v in kwargs.items():
                self.data[symbol][k] = v
            self.save()

# ==========================================
# 2. 策略计算核心 (逻辑复用)
# ==========================================
class StrategyEngine:
    """负责计算指标和生成买卖信号"""
    
    @staticmethod
    def get_indicators(symbol, current_price):
        """
        获取历史数据并拼接当前价格，计算实时指标
        """
        try:
            # 1. 获取历史数据 (过去100天)
            start_date = (datetime.now() - timedelta(days=150)).strftime("%Y%m%d")
            df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start_date, adjust="qfq")
            
            if df.empty: return None

            df = df.rename(columns={'日期':'date', '开盘':'open', '收盘':'close', 
                                  '最高':'high', '最低':'low', '成交量':'volume'})
            df['date'] = pd.to_datetime(df['date'])

            # 2. [关键] 缝合数据：如果今天的日期不在df里（因为akshare可能延迟），或者在交易中
            today = pd.to_datetime(datetime.now().date())
            last_date = df.iloc[-1]['date']

            if last_date < today:
                # 构造今天的临时行
                new_row = {
                    'date': today,
                    'open': current_price, # 近似处理
                    'high': current_price, # 近似
                    'low': current_price,  # 近似
                    'close': current_price,
                    'volume': 0 
                }
                df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            else:
                # 如果今天已经有数据了（比如收盘后），强制更新最后一行收盘价为实时价
                df.iloc[-1, df.columns.get_loc('close')] = current_price

            # 3. 计算指标
            df['ma5'] = df['close'].rolling(5).mean()
            df['ma20'] = df['close'].rolling(20).mean()
            df['std'] = df['close'].rolling(20).std()
            df['upper'] = df['ma20'] + 1.75 * df['std']
            df['lower'] = df['ma20'] - 1.75 * df['std']
            
            # RSI
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))
            
            return df.iloc[-1] # 返回最新一行的指标
        except Exception as e:
            print(f"[Err] 指标计算失败 {symbol}: {e}")
            return None

    @staticmethod
    def analyze(stock_state, indicators):
        """
        输入：单只股票的状态 + 技术指标
        输出：信号 (None, 'BUY', 'SELL'), 详情
        """
        row = indicators
        price = row['close']
        
        stage = stock_state.get('stage', 0)
        avg_cost = stock_state.get('cost', 0)
        max_profit = stock_state.get('max_profit', 0)
        shares = stock_state.get('shares', 0)
        
        # 计算当前浮盈
        current_profit_pct = (price - avg_cost) / avg_cost if avg_cost > 0 else 0
        # 更新最高浮盈 (仅在内存计算，不保存，直到触发逻辑)
        new_max_profit = max(max_profit, current_profit_pct)

        signal = None
        msg = ""
        action_updates = {} # 需要更新的状态

        # --- 买入逻辑 ---
        
        # [扫尾]: 极少仓位且盈利 -> 建议清仓重置
        if shares > 0 and (shares * price < 2000) and price > avg_cost:
            return "SELL", "🧹 扫尾清仓 (零头且盈利)", {'stage': 0, 'shares': 0, 'cost': 0, 'max_profit': 0}

        # [第一枪: 建仓]
        if stage == 0:
            if price < row['lower'] and row['rsi'] < 45:
                return "BUY", "➕ 触发建仓 (破下轨+RSI低)", {'stage': 1}
        
        # [补仓: 第二/三枪]
        elif stage < 3:
            threshold = 0.95 if stage == 1 else 0.90
            is_cost_down = price < avg_cost * threshold
            is_tech_dip = (price < row['lower']) and (row['rsi'] < 40)
            
            if is_cost_down or is_tech_dip:
                reason = "均价摊低" if is_cost_down else "二次探底"
                pct_desc = "30%" if stage == 1 else "50%"
                return "BUY", f"➕ 触发补仓 ({reason}, 建议加{pct_desc})", {'stage': stage + 1}

        # --- 卖出逻辑 ---
        
        if shares > 0:
            # [保本止损]
            if new_max_profit > 0.10 and price < avg_cost * 1.01:
                 return "SELL", "🛡️ 保本离场 (盈利回吐保护)", {'stage': 0, 'shares': 0, 'cost': 0, 'max_profit': 0}

            # [止盈1: 中轨]
            if price > row['ma20'] and current_profit_pct > 0.03:
                # 这里我们假设是全手动操作，只给提示，状态不自动重置，由人去改
                return "SELL", "💰 触及中轨 (建议减仓50%)", {} # 状态不自动变，由人决定

            # [止盈2: 趋势结束]
            if price > row['upper'] and price < row['ma5']:
                return "SELL", "📉 趋势结束 (高位跌破MA5, 建议清仓)", {'stage': 0, 'shares': 0, 'cost': 0, 'max_profit': 0}
            
            # [硬止损]
            if price < avg_cost * 0.85:
                 return "SELL", "☠️ 触发硬止损 (-15%)", {'stage': 0, 'shares': 0, 'cost': 0, 'max_profit': 0}

        # 如果没有信号，但 max_profit 创新高，更新一下状态
        if new_max_profit > max_profit:
             action_updates['max_profit'] = new_max_profit
             return None, None, action_updates

        return None, None, {}

# ==========================================
# 3. 实时数据与通知 (复用你的代码)
# ==========================================
class Pusher:
    def __init__(self, token):
        self.token = token
        self.url = "http://www.pushplus.plus/send"
    def send(self, title, content):
        data = {"token": self.token, "title": title, "content": content, "template": "markdown"}
        try: requests.post(self.url, json=data, timeout=5)
        except: pass

class LiveMonitor:
    def __init__(self):
        self.pusher = Pusher(CONFIG["TOKEN"])
        self.state = StateManager(CONFIG["STATE_FILE"])
        
    def get_realtime_prices(self):
        """批量获取当前价格"""
        symbols = list(self.state.data.keys())
        if not symbols: return {}
        
        # 构造 secids
        secids = ",".join([f"1.{s}" if s.startswith('6') else f"0.{s}" for s in symbols])
        url = "https://push2.eastmoney.com/api/qt/ulist/get"
        params = {"invt": "2", "fields": "f12,f14,f2", "secids": secids} # f2是现价
        
        try:
            resp = requests.get(url, params=params, timeout=5)
            data = resp.json()['data']['diff']
            # 返回字典: {'002415': {'price': 29.5, 'name': '海康'}}
            res_dict = {}
            for item in data:
                if item['f2'] != '-':
                    res_dict[item['f12']] = {'price': float(item['f2']), 'name': item['f14']}
            return res_dict
        except:
            return {}

    def run(self):
        print(f"[System] 智能策略监控启动...")
        print(f"[System] 监控股票: {list(self.state.data.keys())}")
        self.pusher.send("🤖 策略系统上线", "监控已启动")

        while True:
            try:
                # 1. 判断交易时间 (简单版)
                now = datetime.now()
                is_trading = (dtime(9, 25) <= now.time() <= dtime(11, 35)) or (dtime(12, 55) <= now.time() <= dtime(15, 5))
                
                if not is_trading:
                    print(f"\r[Sleep] 休市中... {now.strftime('%H:%M')}", end="")
                    time.sleep(60)
                    continue

                # 2. 获取实时价格
                realtime_data = self.get_realtime_prices()
                if not realtime_data:
                    time.sleep(10)
                    continue
                
                print(f"\n[Scan] {now.strftime('%H:%M:%S')} 扫描 {len(realtime_data)} 只标的...")

                # 3. 逐个分析
                for symbol, rt_info in realtime_data.items():
                    current_price = rt_info['price']
                    name = rt_info['name']
                    stock_state = self.state.get_stock(symbol)
                    
                    # 3.1 获取拼接后的技术指标
                    indicators = StrategyEngine.get_indicators(symbol, current_price)
                    if indicators is None: continue # 数据获取失败
                    
                    # 3.2 策略判决
                    signal, reason, updates = StrategyEngine.analyze(stock_state, indicators)
                    
                    # 打印简报 (可选)
                    # print(f"  > {name}: 现价{current_price} | RSI:{indicators['rsi']:.1f} | 状态:{stock_state['stage']}")

                    # 3.3 触发信号
                    if signal:
                        print(f"  >>> 🚨 触发信号: {name} {signal} {reason}")
                        
                        # 发送推送
                        title = f"{signal} 信号: {name}"
                        content = f"### 策略触发: {name} ({symbol})\n"
                        content += f"**方向**: {signal}\n"
                        content += f"**现价**: {current_price}\n"
                        content += f"**原因**: {reason}\n"
                        content += f"---\n"
                        content += f"RSI: {indicators['rsi']:.1f}\n"
                        content += f"布林下轨: {indicators['lower']:.2f}\n"
                        content += f"当前持仓成本: {stock_state.get('cost', 0)}\n"
                        
                        self.pusher.send(title, content)
                        
                        # 3.4 自动更新状态 (可选)
                        # 如果你希望全是自动的，就在这里 update。
                        # 但实盘建议：只更新 max_profit，买卖操作由人确认后，手动改 json 文件，或者程序里设个标志位
                        if updates:
                            # 仅自动更新 max_profit，不自动改变仓位阶段(stage)，防止误判
                            # 如果你想全自动，把 updates 全部传进去
                            safe_updates = {k:v for k,v in updates.items() if k == 'max_profit'}
                            if safe_updates:
                                self.state.update_stock(symbol, **safe_updates)

                    # 即使没有信号，也要更新 max_profit (如果有变化)
                    elif updates:
                        self.state.update_stock(symbol, **updates)

                # 每次轮询间隔 (建议 60秒，因为计算指标比较耗时)
                time.sleep(60)

            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"[Error] {e}")
                time.sleep(30)

if __name__ == "__main__":
    monitor = LiveMonitor()
    monitor.run()