# =================================================================
# Simplified Analytics Bot - Python Version
# =================================================================
import asyncio
import os
import hmac
import hashlib
import json
import logging
import base64
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import aiohttp
import websockets
from motor.motor_asyncio import AsyncIOMotorClient
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from fastapi import FastAPI
import uvicorn

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =================================================================
# CONFIGURATION
# =================================================================
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
AUTHORIZED_USER_ID = int(os.getenv('AUTHORIZED_USER_ID'))
TARGET_CHANNEL_ID = os.getenv('TARGET_CHANNEL_ID')
MONGO_URI = os.getenv('MONGO_URI')

OKX_CONFIG = {
    'api_key': os.getenv('OKX_API_KEY'),
    'api_secret': os.getenv('OKX_API_SECRET_KEY'),
    'passphrase': os.getenv('OKX_API_PASSPHRASE'),
}

PORT = int(os.getenv('PORT', 3000))

# =================================================================
# DATABASE SETUP
# =================================================================
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client['trading_bot']

async def get_collection(name: str):
    return db[name]

async def get_config(config_id: str, default_value: dict = None):
    if default_value is None:
        default_value = {}
    try:
        collection = await get_collection('configs')
        doc = await collection.find_one({'_id': config_id})
        return doc['data'] if doc else default_value
    except Exception as e:
        logger.error(f"Error getting config {config_id}: {e}")
        return default_value

async def save_config(config_id: str, data: dict):
    try:
        collection = await get_collection('configs')
        await collection.update_one(
            {'_id': config_id},
            {'$set': {'data': data}},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving config {config_id}: {e}")

# Config helpers
async def load_capital():
    config = await get_config('capital', {'value': 0})
    return config.get('value', 0)

async def save_capital(amount: float):
    await save_config('capital', {'value': amount})

async def load_settings():
    return await get_config('settings', {
        'auto_post_to_channel': False,
        'daily_report_time': '22:00'
    })

async def save_settings(settings: dict):
    await save_config('settings', settings)

async def load_positions():
    return await get_config('positions', {})

async def save_positions(positions: dict):
    await save_config('positions', positions)

async def load_balance_state():
    return await get_config('balance_state', {})

async def save_balance_state(state: dict):
    await save_config('balance_state', state)

async def load_history():
    return await get_config('daily_history', [])

async def save_history(history: list):
    await save_config('daily_history', history)

async def save_closed_trade(trade_data: dict):
    try:
        collection = await get_collection('trade_history')
        trade_data['closed_at'] = datetime.now()
        trade_data['_id'] = os.urandom(16).hex()
        await collection.insert_one(trade_data)
    except Exception as e:
        logger.error(f"Error saving closed trade: {e}")

# =================================================================
# OKX API ADAPTER
# =================================================================
class OKXAdapter:
    def __init__(self, config: dict):
        self.base_url = "https://www.okx.com"
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def init_session(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()
    
    async def close_session(self):
        if self.session:
            await self.session.close()
    
    # استبدل هذه الدالة بالكامل
  def get_headers(self, method: str, path: str, body: str = ""):
    timestamp = datetime.utcnow().isoformat()[:-3] + 'Z'
    prehash = timestamp + method.upper() + path + body
    
    # استخدم base64 الذي قمنا باستيراده في الأعلى
    sign_b64 = base64.b64encode(
        hmac.new(
            self.config['api_secret'].encode('utf-8'),
            prehash.encode('utf-8'),
            hashlib.sha26
        ).digest()
    ).decode('utf-8')
    
    return {
        'OK-ACCESS-KEY': self.config['api_key'],
        'OK-ACCESS-SIGN': sign_b64,
        'OK-ACCESS-TIMESTAMP': timestamp,
        'OK-ACCESS-PASSPHRASE': self.config['passphrase'],
        'Content-Type': 'application/json',
    }
    
    async def get_market_prices(self):
        try:
            await self.init_session()
            url = f"{self.base_url}/api/v5/market/tickers?instType=SPOT"
            async with self.session.get(url) as response:
                data = await response.json()
                
                if data.get('code') != '0':
                    return {'error': f"فشل جلب الأسعار: {data.get('msg')}"}
                
                prices = {}
                for ticker in data.get('data', []):
                    if ticker['instId'].endswith('-USDT'):
                        last_price = float(ticker['last'])
                        open_price = float(ticker['open24h'])
                        prices[ticker['instId']] = {
                            'price': last_price,
                            'open24h': open_price,
                            'change24h': (last_price - open_price) / open_price if open_price > 0 else 0,
                            'vol_ccy_24h': float(ticker['volCcy24h'])
                        }
                return prices
        except Exception as e:
            logger.error(f"Error getting market prices: {e}")
            return {'error': "خطأ في الاتصال"}
    
    async def get_portfolio(self, prices: dict):
        try:
            await self.init_session()
            path = "/api/v5/account/balance"
            headers = self.get_headers("GET", path)
            
            async with self.session.get(
                f"{self.base_url}{path}",
                headers=headers
            ) as response:
                data = await response.json()
                
                if data.get('code') != '0':
                    return {'error': f"فشل جلب المحفظة: {data.get('msg')}"}
                
                assets = []
                total = 0
                usdt_value = 0
                
                for asset_data in data['data'][0]['details']:
                    amount = float(asset_data['eq'])
                    if amount > 0:
                        inst_id = f"{asset_data['ccy']}-USDT"
                        price_data = prices.get(inst_id, {
                            'price': 1 if asset_data['ccy'] == 'USDT' else 0,
                            'change24h': 0
                        })
                        value = amount * price_data['price']
                        total += value
                        
                        if asset_data['ccy'] == 'USDT':
                            usdt_value = value
                        
                        if value >= 1:
                            assets.append({
                                'asset': asset_data['ccy'],
                                'price': price_data['price'],
                                'value': value,
                                'amount': amount,
                                'change24h': price_data['change24h']
                            })
                
                assets.sort(key=lambda x: x['value'], reverse=True)
                return {'assets': assets, 'total': total, 'usdt_value': usdt_value}
        except Exception as e:
            logger.error(f"Error getting portfolio: {e}")
            return {'error': "خطأ في الاتصال"}
    
    async def get_balance_for_comparison(self):
        try:
            await self.init_session()
            path = "/api/v5/account/balance"
            headers = self.get_headers("GET", path)
            
            async with self.session.get(
                f"{self.base_url}{path}",
                headers=headers
            ) as response:
                data = await response.json()
                
                if data.get('code') != '0':
                    return None
                
                balances = {}
                for asset_data in data['data'][0]['details']:
                    amount = float(asset_data['eq'])
                    if amount > 0:
                        balances[asset_data['ccy']] = amount
                
                return balances
        except Exception as e:
            logger.error(f"Error getting balance for comparison: {e}")
            return None

# Global adapter instance
okx_adapter = OKXAdapter(OKX_CONFIG)

# =================================================================
# CACHE
# =================================================================
market_cache = {'data': None, 'timestamp': 0}

async def get_cached_market_prices(ttl_ms: int = 15000):
    now = datetime.now().timestamp() * 1000
    if market_cache['data'] and now - market_cache['timestamp'] < ttl_ms:
        return market_cache['data']
    
    data = await okx_adapter.get_market_prices()
    if 'error' not in data:
        market_cache['data'] = data
        market_cache['timestamp'] = now
    return data

# =================================================================
# UTILITY FUNCTIONS
# =================================================================
def format_number(num: float, decimals: int = 2) -> str:
    try:
        return f"{float(num):.{decimals}f}"
    except (ValueError, TypeError):
        return f"{0:.{decimals}f}"

def format_smart(num: float) -> str:
    try:
        n = float(num)
        if not float('inf') > abs(n) > 0:
            return "0.00"
        if abs(n) >= 1:
            return f"{n:.2f}"
        if abs(n) >= 0.01:
            return f"{n:.4f}"
        return f"{n:.4g}"
    except (ValueError, TypeError):
        return "0.00"

def sanitize_markdown_v2(text) -> str:
    if not isinstance(text, (str, int, float)):
        return ''
    text = str(text)
    chars_to_escape = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in chars_to_escape:
        text = text.replace(char, f'\\{char}')
    return text

# =================================================================
# FORMATTING FUNCTIONS (CORRECTED VERSION)
# =================================================================

async def format_portfolio_msg(assets: list, total: float, capital: float) -> str:
    positions = await load_positions()
    usdt_asset = next((a for a in assets if a['asset'] == 'USDT'), {'value': 0})
    cash_percent = (usdt_asset['value'] / total * 100) if total > 0 else 0
    invested_percent = 100 - cash_percent
    pnl = total - capital if capital > 0 else 0
    pnl_percent = (pnl / capital * 100) if capital > 0 else 0
    pnl_sign = '+' if pnl >= 0 else ''
    pnl_emoji = '🟢' if pnl >= 0 else '🔴'

    # استخدم f""" هنا
    caption = f"""🧾 *تقرير المحفظة*
*القيمة الإجمالية:* `${sanitize_markdown_v2(format_number(total))}`"""

    if capital > 0:
        caption += f"""
*رأس المال:* `${sanitize_markdown_v2(format_number(capital))}`
*الربح/الخسارة:* {pnl_emoji} `${sanitize_markdown_v2(pnl_sign)}{sanitize_markdown_v2(format_number(pnl))}` \\(`{sanitize_markdown_v2(pnl_sign)}{sanitize_markdown_v2(format_number(pnl_percent))}%`\\)"""

    caption += f"""
*السيولة:* 💵 {sanitize_markdown_v2(format_number(cash_percent))}% / 📈 {sanitize_markdown_v2(format_number(invested_percent))}%
━━━━━━━━━━━━━━━━━━━━
*الأصول:*"""

    display_assets = [a for a in assets if a['asset'] != 'USDT']
    for asset in display_assets:
        percent = (asset['value'] / total * 100) if total > 0 else 0
        position = positions.get(asset['asset'], {})
        daily_emoji = '🟢' if asset['change24h'] >= 0 else '🔴'
        caption += f"""

*{sanitize_markdown_v2(asset['asset'])}*
  القيمة: `${sanitize_markdown_v2(format_number(asset['value']))}` \\({sanitize_markdown_v2(format_number(percent))}%\\)
  السعر: `${sanitize_markdown_v2(format_smart(asset['price']))}` {daily_emoji} `{sanitize_markdown_v2(format_number(asset['change24h'] * 100))}%`"""

        if position.get('avg_buy_price', 0) > 0:
            asset_pnl = asset['value'] - (position['avg_buy_price'] * asset['amount'])
            cost = position['avg_buy_price'] * asset['amount']
            asset_pnl_percent = (asset_pnl / cost * 100) if cost > 0 else 0
            sign = '+' if asset_pnl >= 0 else ''
            emoji = '🟢' if asset_pnl >= 0 else '🔴'
            caption += f"""
  P/L: {emoji} `{sanitize_markdown_v2(sign)}{sanitize_markdown_v2(format_number(asset_pnl))}` \\(`{sanitize_markdown_v2(sign)}{sanitize_markdown_v2(format_number(asset_pnl_percent))}%`\\)"""

    caption += f"""

*USDT:* `${sanitize_markdown_v2(format_number(usdt_asset['value']))}` \\({sanitize_markdown_v2(format_number(cash_percent))}%\\)"""
    return caption

# لقد أضفت دالة format_private_buy هنا لأنها كانت ناقصة
def format_private_buy(details: dict) -> str:
    asset = details['asset']
    price = details['price']
    amount_change = details['amount_change']
    trade_value = details['trade_value']
    new_asset_weight = details['new_asset_weight']
    new_cash_percent = details['new_cash_percent']
    
    # استخدم f""" هنا
    msg = f"""*🟢 شراء جديد \\| {sanitize_markdown_v2(asset)}*
━━━━━━━━━━━━━━━━━━━━
*السعر:* `${sanitize_markdown_v2(format_smart(price))}`
*الكمية:* `{sanitize_markdown_v2(format_number(abs(amount_change), 6))}`
*القيمة:* `${sanitize_markdown_v2(format_number(trade_value))}`
*الوزن الجديد:* `{sanitize_markdown_v2(format_number(new_asset_weight))}%`
*السيولة المتبقية:* `{sanitize_markdown_v2(format_number(new_cash_percent))}%`"""
    return msg

def format_private_sell(details: dict) -> str:
    asset = details['asset']
    price = details['price']
    amount_change = details['amount_change']
    trade_value = details['trade_value']
    new_asset_weight = details['new_asset_weight']
    new_cash_percent = details['new_cash_percent']
    
    # استخدم f""" هنا
    msg = f"""*🟠 بيع جزئي \\| {sanitize_markdown_v2(asset)}*
━━━━━━━━━━━━━━━━━━━━
*السعر:* `${sanitize_markdown_v2(format_smart(price))}`
*الكمية:* `{sanitize_markdown_v2(format_number(abs(amount_change), 6))}`
*القيمة:* `${sanitize_markdown_v2(format_number(trade_value))}`
*الوزن الجديد:* `{sanitize_markdown_v2(format_number(new_asset_weight))}%`
*السيولة الجديدة:* `{sanitize_markdown_v2(format_number(new_cash_percent))}%`"""
    return msg

def format_private_close(details: dict) -> str:
    asset = details['asset']
    avg_buy_price = details['avg_buy_price']
    avg_sell_price = details['avg_sell_price']
    pnl = details['pnl']
    pnl_percent = details['pnl_percent']
    duration_days = details['duration_days']
    pnl_sign = '+' if pnl >= 0 else ''
    emoji = '🟢' if pnl >= 0 else '🔴'
    
    # استخدم f""" هنا
    msg = f"""*✅ إغلاق مركز \\| {sanitize_markdown_v2(asset)}*
━━━━━━━━━━━━━━━━━━━━
*سعر الشراء:* `${sanitize_markdown_v2(format_smart(avg_buy_price))}`
*سعر البيع:* `${sanitize_markdown_v2(format_smart(avg_sell_price))}`
*النتيجة:* {emoji} `${sanitize_markdown_v2(pnl_sign)}{sanitize_markdown_v2(format_number(pnl))}` \\(`{sanitize_markdown_v2(pnl_sign)}{sanitize_markdown_v2(format_number(pnl_percent))}%`\\)
*المدة:* `{sanitize_markdown_v2(format_number(duration_days, 1))} يوم`"""
    return msg

def format_public_buy(details: dict) -> str:
    asset = details['asset']
    price = details['price']
    
    # استخدم f""" هنا
    msg = f"""*💡 فرصة جديدة 🟢*
تم فتح مركز في *{sanitize_markdown_v2(asset)}*
السعر: `${sanitize_markdown_v2(format_smart(price))}`
📢 @abusalamachart"""
    return msg

def format_public_sell(details: dict) -> str:
    asset = details['asset']
    price = details['price']
    
    # استخدم f""" هنا
    msg = f"""*⚙️ جني أرباح جزئي 🟠*
تم بيع جزء من *{sanitize_markdown_v2(asset)}*
السعر: `${sanitize_markdown_v2(format_smart(price))}`
📢 @abusalamachart"""
    return msg

def format_public_close(details: dict) -> str:
    asset = details['asset']
    pnl_percent = details['pnl_percent']
    avg_buy_price = details['avg_buy_price']
    avg_sell_price = details['avg_sell_price']
    pnl_sign = '+' if pnl_percent >= 0 else ''
    emoji = '🟢' if pnl_percent >= 0 else '🔴'
    
    # استخدم f""" هنا
    msg = f"""*🏆 نتيجة نهائية {emoji}*
*{sanitize_markdown_v2(asset)}*
الدخول: `${sanitize_markdown_v2(format_smart(avg_buy_price))}`
الخروج: `${sanitize_markdown_v2(format_smart(avg_sell_price))}`
النتيجة: `{sanitize_markdown_v2(pnl_sign)}{sanitize_markdown_v2(format_number(pnl_percent))}%`
📢 @abusalamachart"""
    return msg

def format_closed_trade_review(trade: dict, current_price: float) -> str:
    asset = trade['asset']
    avg_buy_price = trade['avg_buy_price']
    avg_sell_price = trade['avg_sell_price']
    quantity = trade['quantity']
    actual_pnl = trade['pnl']
    actual_pnl_percent = trade['pnl_percent']
    
    # استخدم f""" هنا
    msg = f"""*🔍 مراجعة صفقة مغلقة \\| {sanitize_markdown_v2(asset)}*
━━━━━━━━━━━━━━━━━━━━"""
    
    actual_pnl_sign = '+' if actual_pnl >= 0 else ''
    actual_emoji = '🟢' if actual_pnl >= 0 else '🔴'
    msg += f"""
*الأداء الفعلي للصفقة:*
  \\- *سعر الشراء:* `${sanitize_markdown_v2(format_smart(avg_buy_price))}`
  \\- *سعر البيع:* `${sanitize_markdown_v2(format_smart(avg_sell_price))}`
  \\- *النتيجة:* `{sanitize_markdown_v2(actual_pnl_sign)}{sanitize_markdown_v2(format_number(actual_pnl))}` {actual_emoji}
  \\- *العائد:* `{sanitize_markdown_v2(actual_pnl_sign)}{sanitize_markdown_v2(format_number(actual_pnl_percent))}%`"""
    
    hypothetical_pnl = (current_price - avg_buy_price) * quantity
    hypothetical_pnl_percent = ((hypothetical_pnl / (avg_buy_price * quantity)) * 100) if avg_buy_price > 0 else 0
    hypothetical_pnl_sign = '+' if hypothetical_pnl >= 0 else ''
    hypothetical_emoji = '🟢' if hypothetical_pnl >= 0 else '🔴'
    msg += f"""

*لو بقيت الصفقة مفتوحة:*
  \\- *السعر الحالي:* `${sanitize_markdown_v2(format_smart(current_price))}`
  \\- *النتيجة الحالية:* `{sanitize_markdown_v2(hypothetical_pnl_sign)}{sanitize_markdown_v2(format_number(hypothetical_pnl))}` {hypothetical_emoji}
  \\- *العائد الحالي:* `{sanitize_markdown_v2(hypothetical_pnl_sign)}{sanitize_markdown_v2(format_number(hypothetical_pnl_percent))}%`"""
    
    price_change_since_close = current_price - avg_sell_price
    price_change_percent = ((price_change_since_close / avg_sell_price) * 100) if avg_sell_price > 0 else 0
    change_sign = '⬆️' if price_change_since_close >= 0 else '⬇️'
    msg += f"""

*تحليل قرار الخروج:*
  \\- *حركة السعر منذ الإغلاق:* `{sanitize_markdown_v2(format_number(price_change_percent))}%` {change_sign}"""
    
    if price_change_since_close > 0:
        msg += f"""
  \\- *التقييم:* 📈 السعر واصل الصعود، كان ممكن ربح أكبر"""
    else:
        msg += f"""
  \\- *التقييم:* ✅ قرار ممتاز، السعر انخفض بعد الخروج"""
    return msg

async def format_daily_copy_report() -> str:
    twenty_four_hours_ago = datetime.now() - timedelta(days=1)
    collection = await get_collection('trade_history')
    cursor = collection.find({'closed_at': {'$gte': twenty_four_hours_ago}})
    closed_trades = await cursor.to_list(length=None)
    
    if not closed_trades:
        return "📊 لم يتم إغلاق أي صفقات في الـ 24 ساعة الماضية\\."
    
    today = datetime.now()
    date_string = today.strftime('%d/%m/%Y')
    
    # استخدم f""" هنا
    report = f"""📊 تقرير النسخ اليومي – خلال الـ24 ساعة الماضية
🗓 التاريخ: {date_string}
"""
    
    total_pnl_weighted_sum = 0
    total_weight = 0
    for trade in closed_trades:
        if 'pnl_percent' not in trade or 'entry_capital_percent' not in trade:
            continue
        result_emoji = '🔼' if trade['pnl_percent'] >= 0 else '🔽'
        pnl_sign = '+' if trade['pnl_percent'] >= 0 else ''
        report += f"""
🔸 اسم العملة: {trade['asset']}
🔸 نسبة الدخول من رأس المال: {format_number(trade['entry_capital_percent'])}%
🔸 متوسط سعر الشراء: {format_smart(trade['avg_buy_price'])}
🔸 سعر الخروج: {format_smart(trade['avg_sell_price'])}
🔸 نسبة الخروج من الكمية: {format_number(trade.get('exit_quantity_percent', 100))}%
🔸 النتيجة: {pnl_sign}{format_number(trade['pnl_percent'])}% {result_emoji}"""
        
        if trade['entry_capital_percent'] > 0:
            total_pnl_weighted_sum += trade['pnl_percent'] * trade['entry_capital_percent']
            total_weight += trade['entry_capital_percent']
    
    total_pnl = total_pnl_weighted_sum / total_weight if total_weight > 0 else 0
    total_pnl_emoji = '📈' if total_pnl >= 0 else '📉'
    total_pnl_sign = '+' if total_pnl >= 0 else ''
    report += f"""

إجمالي الربح الحالي خدمة النسخ: {total_pnl_sign}{format_number(total_pnl, 2)}% {total_pnl_emoji}
✍️ يمكنك الدخول في أي وقت تراه مناسب، الخدمة مفتوحة للجميع
📢 قناة التحديثات الرسمية:
@abusalamachart
🌐 رابط النسخ المباشر:
🏦 https://t.me/abusalamachart"""
    return report
# =================================================================
# POSITION TRACKING
# =================================================================
async def update_position_and_analyze(
    asset: str,
    amount_change: float,
    price: float,
    new_total_amount: float,
    old_total_value: float
) -> dict:
    if not asset or price is None or price != price:  # NaN check
        return {'analysis_result': None}
    
    positions = await load_positions()
    position = positions.get(asset, {})
    analysis_result = {'type': 'none', 'data': {}}
    
    if amount_change > 0:  # Buy
        trade_value = amount_change * price
        entry_capital_percent = (trade_value / old_total_value * 100) if old_total_value > 0 else 0
        
        if not position:
            positions[asset] = {
                'total_amount_bought': amount_change,
                'total_cost': trade_value,
                'avg_buy_price': price,
                'open_date': datetime.now().isoformat(),
                'total_amount_sold': 0,
                'realized_value': 0,
                'entry_capital_percent': entry_capital_percent
            }
            position = positions[asset]
        else:
            position['total_amount_bought'] += amount_change
            position['total_cost'] += trade_value
            position['avg_buy_price'] = position['total_cost'] / position['total_amount_bought']
        
        analysis_result['type'] = 'buy'
        
    elif amount_change < 0 and position:  # Sell
        sold_amount = abs(amount_change)
        position['realized_value'] = position.get('realized_value', 0) + (sold_amount * price)
        position['total_amount_sold'] = position.get('total_amount_sold', 0) + sold_amount
        
        if new_total_amount * price < 1:  # Close position
            avg_sell_price = position['realized_value'] / position['total_amount_sold'] if position['total_amount_sold'] > 0 else 0
            quantity = position['total_amount_bought']
            invested_capital = position['total_cost']
            final_pnl = (avg_sell_price - position['avg_buy_price']) * quantity
            final_pnl_percent = (final_pnl / invested_capital * 100) if invested_capital > 0 else 0
            
            close_date = datetime.now()
            open_date = datetime.fromisoformat(position['open_date'])
            duration_days = (close_date - open_date).total_seconds() / (24 * 60 * 60)
            
            close_data = {
                'asset': asset,
                'pnl': final_pnl,
                'pnl_percent': final_pnl_percent,
                'duration_days': duration_days,
                'avg_buy_price': position['avg_buy_price'],
                'avg_sell_price': avg_sell_price,
                'quantity': quantity,
                'entry_capital_percent': position.get('entry_capital_percent', 0),
                'exit_quantity_percent': 100
            }
            
            await save_closed_trade(close_data)
            analysis_result = {'type': 'close', 'data': close_data}
            del positions[asset]
        else:
            analysis_result['type'] = 'sell'
    
    await save_positions(positions)
    analysis_result['data']['position'] = positions.get(asset, position)
    return {'analysis_result': analysis_result}

# =================================================================
# BALANCE MONITORING
# =================================================================
is_processing_balance = False

async def monitor_balance_changes(bot: Bot):
    global is_processing_balance
    if is_processing_balance:
        return
    is_processing_balance = True
    
    try:
        previous_state = await load_balance_state()
        previous_balances = previous_state.get('balances', {})
        current_balance = await okx_adapter.get_balance_for_comparison()
        
        if not current_balance:
            raise Exception("فشل جلب الرصيد")
        
        prices = await get_cached_market_prices()
        if 'error' in prices:
            raise Exception("فشل جلب الأسعار")
        
        portfolio_data = await okx_adapter.get_portfolio(prices)
        if 'error' in portfolio_data:
            raise Exception(portfolio_data['error'])
        
        new_assets = portfolio_data['assets']
        new_total_value = portfolio_data['total']
        new_usdt_value = portfolio_data['usdt_value']
        
        # Initial setup
        if not previous_balances:
            await save_balance_state({
                'balances': current_balance,
                'total_value': new_total_value
            })
            is_processing_balance = False
            return
        
        # Check for changes
        all_assets = set(list(previous_balances.keys()) + list(current_balance.keys()))
        state_needs_update = False
        
        for asset in all_assets:
            if asset == 'USDT':
                continue
            
            prev_amount = previous_balances.get(asset, 0)
            curr_amount = current_balance.get(asset, 0)
            difference = curr_amount - prev_amount
            
            price_data = prices.get(f"{asset}-USDT", {})
            if not price_data or abs(difference * price_data.get('price', 0)) < 1:
                continue
            
            state_needs_update = True
            
            old_total_value = previous_state.get('total_value', 0)
            result = await update_position_and_analyze(
                asset, difference, price_data['price'], curr_amount, old_total_value
            )
            analysis_result = result['analysis_result']
            
            if analysis_result['type'] == 'none':
                continue
            
            trade_value = abs(difference) * price_data['price']
            new_asset_data = next((a for a in new_assets if a['asset'] == asset), None)
            new_asset_value = new_asset_data['value'] if new_asset_data else 0
            new_asset_weight = (new_asset_value / new_total_value * 100) if new_total_value > 0 else 0
            new_cash_percent = (new_usdt_value / new_total_value * 100) if new_total_value > 0 else 0
            
            base_details = {
                'asset': asset,
                'price': price_data['price'],
                'amount_change': difference,
                'trade_value': trade_value,
                'old_total_value': old_total_value,
                'new_asset_weight': new_asset_weight,
                'new_usdt_value': new_usdt_value,
                'new_cash_percent': new_cash_percent,
                'position': analysis_result['data'].get('position', {})
            }
            
            settings = await load_settings()
            
            if analysis_result['type'] == 'buy':
                private_message = format_private_buy(base_details)
                public_message = format_public_buy(base_details)
                await bot.send_message(
                    AUTHORIZED_USER_ID,
                    private_message,
                    parse_mode='MarkdownV2'
                )
                if settings.get('auto_post_to_channel', False):
                    await bot.send_message(
                        TARGET_CHANNEL_ID,
                        public_message,
                        parse_mode='MarkdownV2'
                    )
            
            elif analysis_result['type'] == 'sell':
                private_message = format_private_sell(base_details)
                public_message = format_public_sell(base_details)
                await bot.send_message(
                    AUTHORIZED_USER_ID,
                    private_message,
                    parse_mode='MarkdownV2'
                )
                if settings.get('auto_post_to_channel', False):
                    await bot.send_message(
                        TARGET_CHANNEL_ID,
                        public_message,
                        parse_mode='MarkdownV2'
                    )
            
            elif analysis_result['type'] == 'close':
                private_message = format_private_close(analysis_result['data'])
                public_message = format_public_close(analysis_result['data'])
                if settings.get('auto_post_to_channel', False):
                    await bot.send_message(
                        TARGET_CHANNEL_ID,
                        public_message,
                        parse_mode='MarkdownV2'
                    )
                await bot.send_message(
                    AUTHORIZED_USER_ID,
                    private_message,
                    parse_mode='MarkdownV2'
                )
        
        if state_needs_update:
            await save_balance_state({
                'balances': current_balance,
                'total_value': new_total_value
            })
    
    except Exception as e:
        logger.error(f"Error in monitor_balance_changes: {e}")
    finally:
        is_processing_balance = False

# =================================================================
# BACKGROUND JOBS
# =================================================================
async def run_daily_jobs():
    try:
        prices = await get_cached_market_prices()
        if 'error' in prices:
            return
        
        portfolio_data = await okx_adapter.get_portfolio(prices)
        if 'error' in portfolio_data:
            return
        
        total = portfolio_data['total']
        history = await load_history()
        date = datetime.now().strftime('%Y-%m-%d')
        
        existing = next((h for h in history if h.get('date') == date), None)
        if existing:
            existing['total'] = total
        else:
            history.append({'date': date, 'total': total, 'time': datetime.now().timestamp() * 1000})
        
        if len(history) > 35:
            history.pop(0)
        
        await save_history(history)
        logger.info(f"Daily record saved: {date} - ${format_number(total)}")
    except Exception as e:
        logger.error(f"Error in run_daily_jobs: {e}")

async def run_daily_report_job(bot: Bot):
    try:
        logger.info("Running daily report job...")
        report = await format_daily_copy_report()
        safe_report = sanitize_markdown_v2(report)
        
        if "لم يتم إغلاق أي صفقات" in report:
            await bot.send_message(
                AUTHORIZED_USER_ID,
                safe_report,
                parse_mode='MarkdownV2'
            )
        else:
            await bot.send_message(
                TARGET_CHANNEL_ID,
                safe_report,
                parse_mode='MarkdownV2'
            )
            await bot.send_message(
                AUTHORIZED_USER_ID,
                "✅ تم إرسال تقرير النسخ اليومي إلى القناة بنجاح\\.",
                parse_mode='MarkdownV2'
            )
    except Exception as e:
        logger.error(f"Error in run_daily_report_job: {e}")
        await bot.send_message(
            AUTHORIZED_USER_ID,
            f"❌ حدث خطأ أثناء إنشاء التقرير اليومي: {sanitize_markdown_v2(str(e))}",
            parse_mode='MarkdownV2'
        )

# =================================================================
# WEBSOCKET
# =================================================================
balance_check_debounce_timer = None

async def connect_to_okx_socket(bot: Bot):
    uri = 'wss://ws.okx.com:8443/ws/v5/private'
    
    while True:
        try:
            async with websockets.connect(uri) as ws:
                logger.info("OKX WebSocket Connected")
                
                # Authenticate
                timestamp = str(int(datetime.now().timestamp()))
                prehash = timestamp + 'GET' + '/users/self/verify'
                sign = base64.b64encode(
                    hmac.new(
                        OKX_CONFIG['api_secret'].encode('utf-8'),
                        prehash.encode('utf-8'),
                        hashlib.sha256
                    ).digest()
                ).decode('utf-8')
                
                auth_msg = {
                    "op": "login",
                    "args": [{
                        "apiKey": OKX_CONFIG['api_key'],
                        "passphrase": OKX_CONFIG['passphrase'],
                        "timestamp": timestamp,
                        "sign": sign,
                    }]
                }
                await ws.send(json.dumps(auth_msg))
                
                # Ping task
                async def ping_task():
                    while True:
                        try:
                            await ws.send('ping')
                            await asyncio.sleep(25)
                        except:
                            break
                
                ping = asyncio.create_task(ping_task())
                
                # Message handler
                async for message in ws:
                    if message == 'pong':
                        continue
                    
                    try:
                        data = json.loads(message)
                        
                        if data.get('event') == 'login' and data.get('code') == '0':
                            logger.info("WebSocket Authenticated")
                            subscribe_msg = {
                                "op": "subscribe",
                                "args": [{"channel": "account"}]
                            }
                            await ws.send(json.dumps(subscribe_msg))
                        
                        if data.get('arg', {}).get('channel') == 'account' and data.get('data'):
                            global balance_check_debounce_timer
                            if balance_check_debounce_timer:
                                balance_check_debounce_timer.cancel()
                            balance_check_debounce_timer = asyncio.create_task(
                                debounced_balance_check(bot)
                            )
                    except Exception as e:
                        logger.error(f"Error processing WebSocket message: {e}")
                
                ping.cancel()
        
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            await asyncio.sleep(5)

async def debounced_balance_check(bot: Bot):
    await asyncio.sleep(5)
    await monitor_balance_changes(bot)

# =================================================================
# BOT SETUP
# =================================================================
class Form(StatesGroup):
    set_capital = State()

# Create bot and dispatcher
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Keyboards
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 المحفظة"), KeyboardButton(text="🔍 مراجعة الصفقات")],
        [KeyboardButton(text="⚙️ الإعدادات")]
    ],
    resize_keyboard=True
)

# Authorization middleware
@dp.message.middleware()
async def auth_middleware(handler, event, data):
    if event.from_user.id == AUTHORIZED_USER_ID:
        return await handler(event, data)
    return

# Start command
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🤖 *أهلاً بك في البوت*",
        parse_mode='MarkdownV2',
        reply_markup=main_keyboard
    )

# Portfolio handler
@dp.message(F.text == "📊 المحفظة")
async def show_portfolio(message: types.Message):
    loading = await message.answer("⏳ جاري التحميل...")
    
    try:
        prices = await get_cached_market_prices()
        if 'error' in prices:
            raise Exception(prices['error'])
        
        capital = await load_capital()
        portfolio_data = await okx_adapter.get_portfolio(prices)
        if 'error' in portfolio_data:
            raise Exception(portfolio_data['error'])
        
        caption = await format_portfolio_msg(
            portfolio_data['assets'],
            portfolio_data['total'],
            capital
        )
        
        await loading.edit_text(caption, parse_mode='MarkdownV2')
    except Exception as e:
        await loading.edit_text(
            f"❌ خطأ: {sanitize_markdown_v2(str(e))}",
            parse_mode='MarkdownV2'
        )

# Trade review handler
@dp.message(F.text == "🔍 مراجعة الصفقات")
async def review_trades(message: types.Message):
    loading = await message.answer("⏳ جاري جلب الصفقات المغلقة...")
    
    try:
        collection = await get_collection('trade_history')
        cursor = collection.find({'quantity': {'$exists': True}}).sort('closed_at', -1).limit(5)
        closed_trades = await cursor.to_list(length=5)
        
        if not closed_trades:
            await loading.edit_text(
                "ℹ️ لا يوجد سجل صفقات مغلقة لمراجعتها\\.",
                parse_mode='MarkdownV2'
            )
            return
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{trade['asset']} | ${format_smart(trade['avg_sell_price'])}",
                callback_data=f"review_{trade['_id']}"
            )]
            for trade in closed_trades
        ])
        
        await loading.edit_text(
            "👇 *اختر صفقة من القائمة لمراجعتها:*",
            parse_mode='MarkdownV2',
            reply_markup=keyboard
        )
    except Exception as e:
        await loading.edit_text(
            f"❌ خطأ: {sanitize_markdown_v2(str(e))}",
            parse_mode='MarkdownV2'
        )

# Settings handler
@dp.message(F.text == "⚙️ الإعدادات")
async def show_settings(message: types.Message):
    settings = await load_settings()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 تعيين رأس المال", callback_data="set_capital"),
            InlineKeyboardButton(
                text=f"🚀 النشر: {'✅' if settings.get('auto_post_to_channel') else '❌'}",
                callback_data="toggle_post"
            )
        ],
        [InlineKeyboardButton(text="📊 إرسال تقرير النسخ", callback_data="send_report")]
    ])
    
    await message.answer(
        "⚙️ *الإعدادات*",
        parse_mode='MarkdownV2',
        reply_markup=keyboard
    )

# Callback handlers
@dp.callback_query(F.data.startswith("review_"))
async def handle_trade_review(callback: types.CallbackQuery):
    await callback.answer()
    trade_id = callback.data.split('_')[1]
    
    await callback.message.edit_text("⏳ جاري تحليل الصفقة...")
    
    try:
        collection = await get_collection('trade_history')
        trade = await collection.find_one({'_id': trade_id})
        
        if not trade or 'quantity' not in trade:
            await callback.message.edit_text(
                "❌ لم يتم العثور على الصفقة\\.",
                parse_mode='MarkdownV2'
            )
            return
        
        prices = await get_cached_market_prices()
        current_price = prices.get(f"{trade['asset']}-USDT", {}).get('price')
        
        if not current_price:
            await callback.message.edit_text(
                f"❌ تعذر جلب السعر الحالي لـ {sanitize_markdown_v2(trade['asset'])}\\.",
                parse_mode='MarkdownV2'
            )
            return
        
        review_message = format_closed_trade_review(trade, current_price)
        await callback.message.edit_text(review_message, parse_mode='MarkdownV2')
    except Exception as e:
        await callback.message.edit_text(
            f"❌ خطأ: {sanitize_markdown_v2(str(e))}",
            parse_mode='MarkdownV2'
        )

@dp.callback_query(F.data == "set_capital")
async def set_capital_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text("💰 أرسل المبلغ الجديد:")
    await state.set_state(Form.set_capital)

@dp.message(Form.set_capital)
async def process_capital(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 0:
            raise ValueError()
        
        await save_capital(amount)
        await message.answer(
            f"✅ تم تحديث رأس المال إلى: `${sanitize_markdown_v2(format_number(amount))}`",
            parse_mode='MarkdownV2'
        )
    except ValueError:
        await message.answer("❌ مبلغ غير صالح")
    finally:
        await state.clear()

@dp.callback_query(F.data == "toggle_post")
async def toggle_post_callback(callback: types.CallbackQuery):
    await callback.answer()
    
    settings = await load_settings()
    settings['auto_post_to_channel'] = not settings.get('auto_post_to_channel', False)
    await save_settings(settings)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 تعيين رأس المال", callback_data="set_capital"),
            InlineKeyboardButton(
                text=f"🚀 النشر: {'✅' if settings['auto_post_to_channel'] else '❌'}",
                callback_data="toggle_post"
            )
        ],
        [InlineKeyboardButton(text="📊 إرسال تقرير النسخ", callback_data="send_report")]
    ])
    
    await callback.message.edit_reply_markup(reply_markup=keyboard)

@dp.callback_query(F.data == "send_report")
async def send_report_callback(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("⏳ جاري إنشاء التقرير اليومي...")
    
    await run_daily_report_job(bot)
    await callback.message.edit_text(
        "✅ تم إرسال التقرير بنجاح\\!",
        parse_mode='MarkdownV2'
    )

# =================================================================
# FASTAPI SERVER
# =================================================================
app = FastAPI()

@app.get("/healthcheck")
async def healthcheck():
    return {"status": "OK"}

# =================================================================
# MAIN
# =================================================================
async def main():
    # Start background tasks
    asyncio.create_task(connect_to_okx_socket(bot))
    
    # Schedule daily jobs
    async def daily_job_scheduler():
        while True:
            await run_daily_jobs()
            await asyncio.sleep(24 * 60 * 60)
    
    async def daily_report_scheduler():
        while True:
            await run_daily_report_job(bot)
            await asyncio.sleep(24 * 60 * 60)
    
    asyncio.create_task(daily_job_scheduler())
    asyncio.create_task(daily_report_scheduler())
    
    # Start initial jobs
    await run_daily_jobs()
    
    # Send startup message
    await bot.send_message(
        AUTHORIZED_USER_ID,
        "✅ *البوت جاهز مع تقرير النسخ ومراجعة الصفقات*",
        parse_mode='MarkdownV2'
    )
    
    # Start polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    # Run FastAPI in background
    import threading
    threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=PORT),
        daemon=True
    ).start()
    
    # Run bot
    asyncio.run(main())
