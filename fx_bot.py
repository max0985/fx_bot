from collections import defaultdict
from datetime import datetime, timedelta
import calendar
import os
import re
import io
import calendar
import logging
import pandas as pd
import time
from sqlalchemy.exc import OperationalError
import random
from io import BytesIO
from logging.handlers import RotatingFileHandler
from decimal import Decimal, getcontext
from sqlalchemy import create_engine, Column, String, Float, DateTime, Integer, ForeignKey, func, text
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session, relationship
from telegram import Update
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import Numeric
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

# ================== 初始化配置 ==================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
getcontext().prec = 8
Base = declarative_base()

# ================== 数据库模型 ==================
class Customer(Base):
    __tablename__ = 'customers'
    name = Column(String(50), primary_key=True)
    wallet = Column(String(34))
    balances = relationship("Balance", back_populates="customer")

class Balance(Base):
    __tablename__ = 'balances'
    id = Column(Integer, primary_key=True)
    customer_name = Column(String(50), ForeignKey('customers.name'))
    currency = Column(String(4))
    amount = Column(Float)
    customer = relationship("Customer", back_populates="balances")

class Transaction(Base):
    __tablename__ = 'transactions'
    order_id = Column(String(12), primary_key=True)
    customer_name = Column(String(50))
    transaction_type = Column(String(4))    # buy/sell
    base_currency = Column(String(4))      # 目标货币
    quote_currency = Column(String(4))     # 支付货币
    amount = Column(Float)                 # 目标货币数量
    rate = Column(Float)                   # 报价汇率
    operator = Column(String(1))          # 新增：运算符（/ 或 *）
    status = Column(String(20), default='pending')  # 交易状态
    payment_in = Column(Float, default=0)   # 已收金额
    payment_out = Column(Float, default=0)  # 已付金额
    timestamp = Column(DateTime, default=datetime.now)
    settled_in = Column(Float, default=0)  
    settled_out = Column(Float, default=0) # 新增：已结算付款

class Adjustment(Base):
    __tablename__ = 'adjustments'
    id = Column(Integer, primary_key=True)
    customer_name = Column(String(50))
    currency = Column(String(4))
    amount = Column(Float)
    note = Column(String(200))
    timestamp = Column(DateTime, default=datetime.now)

class Expense(Base):
    __tablename__ = 'expenses'
    id = Column(Integer, primary_key=True)
    amount = Column(Float)
    currency = Column(String(4))
    purpose = Column(String(200))
    timestamp = Column(DateTime, default=datetime.now)

# ================== 数据库初始化 ==================
engine = create_engine('sqlite:///fx_bot.db', pool_pre_ping=True, connect_args={'timeout': 30})
Base.metadata.create_all(engine)
session_factory = sessionmaker(bind=engine)
Session = scoped_session(session_factory)


# ================== 数据库迁移脚本 ==================
def run_migrations():
    engine = create_engine('sqlite:///fx_bot.db')
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN settled_in FLOAT DEFAULT 0"))
            conn.execute(text("ALTER TABLE transactions ADD COLUMN settled_out FLOAT DEFAULT 0"))
            conn.commit()
            logger.info("数据库迁移成功")
        except Exception as e:
            logger.warning("数据库迁移可能已经完成: %s", str(e))

# ================== 核心工具函数 ==================
def setup_logging():
    """配置日志系统"""
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "fx_bot.log")
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3),
            logging.StreamHandler()
        ]
    )
    logger.info("日志系统初始化完成")

def generate_order_id(session):
    """生成递增订单号"""
    last_order = session.query(Transaction).order_by(Transaction.order_id.desc()).first()
    if last_order:
        last_num = int(last_order.order_id[2:])
        return f"YS{last_num + 1:09d}"
    return "YS000000001"

def update_balance(session, customer: str, currency: str, amount: float):
    """安全的余额更新（支持4位货币代码）"""
    try:
        # 确保客户记录存在
        customer_obj = session.query(Customer).filter_by(name=customer).first()
        if not customer_obj:
            customer_obj = Customer(name=customer)
            session.add(customer_obj)
            session.flush()  # 立即写入数据库但不提交事务

        currency = currency.upper()  # 移除截断，保留完整货币代码
        balance = session.query(Balance).filter_by(
            customer_name=customer,
            currency=currency
        ).with_for_update().first()

        new_amount = round(amount, 2)
        if balance:
            balance.amount = round(balance.amount + new_amount, 2)
        else:
            balance = Balance(
                customer_name=customer,
                currency=currency,
                amount=new_amount
            )
            session.add(balance)
        logger.info(f"余额更新: {customer} {currency} {new_amount:+}")
    except Exception as e:
        logger.error(f"余额更新失败: {str(e)}")
        raise

def parse_date_range(date_str: str):
    """解析日期范围字符串"""
    try:
        start_str, end_str = date_str.split('-')
        start_date = datetime.strptime(start_str.strip(), '%d/%m/%Y')
        end_date = datetime.strptime(end_str.strip(), '%d/%m/%Y')
        # 将结束日期设置为当天的23:59:59
        end_date = end_date.replace(hour=23, minute=59, second=59)
        return start_date, end_date
    except Exception as e:
        raise ValueError("日期格式错误，请使用 DD/MM/YYYY-DD/MM/YYYY 格式")

# ================== Excel报表生成工具函数 ==================
def generate_excel_buffer(df_dict: dict, sheet_names: list) -> BytesIO:
    """生成Excel文件内存缓冲"""
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        for idx, df in enumerate(df_dict.values()):
            df.to_excel(writer, sheet_name=sheet_names[idx], index=False)
            # 自动调整列宽
            worksheet = writer.sheets[sheet_names[idx]]
            for column in df:
                column_width = max(df[column].astype(str).map(len).max(), len(column)) + 2
                col_idx = df.columns.get_loc(column)
                worksheet.column_dimensions[chr(65 + col_idx)].width = column_width
    output.seek(0)
    return output

# 通用状态判断函数
def get_tx_status(tx):
    if tx.operator == '/':
        total_quote = tx.amount / tx.rate
    else:
        total_quote = tx.amount * tx.rate

    # 获取结算金额
    settled_base = tx.settled_out if tx.transaction_type == 'buy' else tx.settled_in
    settled_quote = tx.settled_in if tx.transaction_type == 'buy' else tx.settled_out
    
    # 计算整数部分
    base_done = int(settled_base) >= int(tx.amount)
    quote_done = int(settled_quote) >= int(total_quote)
    
    # 计算进度百分比
    base_progress = settled_base / tx.amount if tx.amount != 0 else 0
    quote_progress = settled_quote / total_quote if total_quote != 0 else 0
    min_progress = min(base_progress, quote_progress)
    
    # 状态判断
    if base_done and quote_done:
        return "已完成", min_progress
    elif min_progress > 0:
        return f"部分结算 ({min_progress:.1%})", min_progress
    else:
        return "未结算", min_progress
    
# ================== 交易处理模块 ==================
async def handle_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理交易指令"""
    session = Session()
    try:
        text = update.message.text.strip()
        logger.info(f"收到交易指令: {text}")

        # 修正后的正则表达式
        pattern = (
            r'^(\w+)\s+'  # 客户名
            r'(买|卖|buy|sell)\s+'  # 交易类型
            r'([\d,]+(?:\.\d*)?)([A-Za-z]{3,4})\s*'  # 金额和基础货币（支持小数）
            r'([/*])\s*'  # 运算符
            r'([\d.]+)\s+'  # 汇率
            r'([A-Za-z]{3,4})$'  # 报价货币
        )
        match = re.match(pattern, text, re.IGNORECASE)

        if not match:
            logger.error(f"格式不匹配：{text}")
            await update.message.reply_text(
                "❌ 格式错误！正确示例：\n"
                "`客户A 买 10000USD/4.42 USDT`\n"
                "`客户B 卖 5000EUR*3.45 GBP`\n"
                "`客户C 买 5678MYR/4.42 USDT`（支持无空格）"
            )
            return

        # 解析参数（调整分组索引）
        customer = match.group(1)
        action = match.group(2).lower()
        amount_str = re.sub(r'[^\d.]', '', match.group(3))  # 增强容错处理
        base_currency = match.group(4).upper()
        operator = match.group(5)
        rate_str = match.group(6)
        quote_currency = match.group(7).upper()

        logger.info(f"解析结果: {customer}, {action}, {amount_str}, {base_currency}, {operator}, {rate_str}, {quote_currency}")

        # 类型转换和计算
        try:
            amount = float(amount_str)
            rate = float(rate_str)
            quote_amount = amount / rate if operator == '/' else amount * rate
        except Exception as e:
            await update.message.reply_text(f"❌ 数值错误：{str(e)}")
            return

        # 关键修复：交易方向逻辑
        if action in ('买', 'buy'):
            transaction_type = 'buy'
            # 客户应支付报价货币（USDT），获得基础货币（MYR）
            receive_currency = base_currency   # 客户收到的货币
            pay_currency = quote_currency      # 客户需要支付的货币
        else:
            transaction_type = 'sell'
            # 客户应支付基础货币（MYR），获得报价货币（USDT）
            receive_currency = quote_currency  # 客户收到的货币
            pay_currency = base_currency       # 客户需要支付的货币

        # 创建交易记录
        order_id = generate_order_id(session)
        new_tx = Transaction(
            order_id=order_id,
            customer_name=customer,
            transaction_type=transaction_type,
            base_currency=base_currency,
            quote_currency=quote_currency,
            amount=amount,
            rate=rate,
            status='pending',
            operator=operator,  
            payment_in=0,
            payment_out=0,
            settled_in=0,
            settled_out=0
        )
        session.add(new_tx)

        # 关键修改：更新余额逻辑
        with session.begin_nested():
            session.add(new_tx)
            if transaction_type == 'buy':
                received_curr = quote_currency
                paid_curr = base_currency
                payment_amount = quote_amount
                received_amount = amount
                # 客户获得基础货币（MYR），支付报价货币（USDT）
                update_balance(session, customer, base_currency, amount)
                update_balance(session, customer, quote_currency, -quote_amount)
            else:
                received_curr = base_currency
                paid_curr = quote_currency
                payment_amount = amount
                received_amount = quote_amount
                # 客户支付基础货币（MYR），获得报价货币（USDT）
                update_balance(session, customer, base_currency, -amount)
                update_balance(session, customer, quote_currency, quote_amount)
        
        session.commit()

        # 成功响应（保持原格式）
        await update.message.reply_text(
            f"✅ *交易成功创建* 🎉\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"▪️ 客户：{customer}\n"
            f"▪️ 单号：`{order_id}`\n"
            f"▪️ 类型：{'买入' if transaction_type == 'buy' else '卖出'}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💱 *汇率说明*\n"
            f"1 {quote_currency} = {rate:.4f} {base_currency}\n\n"
    
            f"📥 *客户需要支付*：\n"
            f"- {payment_amount:,.2f} {pay_currency}\n"
            f"📤 *客户将获得*：\n" 
            f"- {received_amount:,.2f} {receive_currency}\n\n"
    
            f"🏦 *公司账务变动*：\n"
            f"▸ 收入：{payment_amount:,.2f} {pay_currency}\n"
            f"▸ 支出：{received_amount:,.2f} {receive_currency}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔧 *后续操作指引*\n"
            f"1️⃣ 当收到客户款项时：\n"
            f"   `/received {customer} {payment_amount:.2f}{pay_currency}`\n\n"
            f"2️⃣ 当向客户支付时：\n"
            f"   `/paid {customer} {received_amount:.2f}{receive_currency}`\n\n"
            f"📝 支持分次操作，金额可修改"
            
        )

    except Exception as e:
        session.rollback()
        logger.error(f"交易处理失败：{str(e)}", exc_info=True)
        await update.message.reply_text(
            "❌ 交易创建失败！\n"
            "⚠️ 错误详情请查看日志"
        )
    finally:
        Session.remove()

async def handle_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理客户付款（直接增加公司余额，减少客户余额）"""
    session = Session()
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("❌ 参数错误！格式: /received [客户] [金额+货币]")
            return

        customer, amount_curr = args[0], args[1]
        
        # 解析金额和货币
        try:
            amount = float(re.sub(r'[^\d.]', '', amount_curr))
            currency = re.search(r'[A-Za-z]{3,4}', amount_curr, re.I).group().upper()
        except (ValueError, AttributeError):
            await update.message.reply_text("❌ 金额格式错误！示例: /received 客户A 1000USD")
            return

        # ✅ 直接更新余额
        with session.begin_nested():
            update_balance(session, customer, currency, amount)  # 客户支付，余额减少
            update_balance(session, 'COMPANY', currency, amount)  # 公司收到，余额增加

        tx = session.query(Transaction).filter(
            Transaction.customer_name == customer,
            (
                (Transaction.transaction_type == 'buy') & (Transaction.quote_currency == currency) |
                (Transaction.transaction_type == 'sell') & (Transaction.base_currency == currency)
            ),
            Transaction.status.in_(['pending', 'partial'])
        ).order_by(Transaction.timestamp.desc()).first()
        if tx:
            with session.begin_nested():
                tx.settled_in += amount  # Add to settled_in rather than setting it
                if tx.transaction_type == 'buy':
                    total_quote = tx.amount / tx.rate if tx.operator == '/' else tx.amount * tx.rate
                    if tx.settled_in >= total_quote:
                        tx.status = 'settled'
                    else:
                        tx.status = 'partial'
                elif tx.transaction_type == 'sell':
                    if tx.settled_out >= (tx.amount / tx.rate if tx.operator == '/' else tx.amount * tx.rate):
                       tx.status = 'settled'
                    else:
                       tx.status = 'partial'
            session.commit()
        else:
            logger.warning(f"No matching transaction found for {customer} and {currency}")

        # 构建响应
        response = [
            f"✅ 成功处理{customer}付款 {amount:,.2f}{currency}",
            "━━━━━━━━━━━━━━━━━━",
            f"▸ 客户 {customer} {currency} 余额减少 {amount:,.2f}",
            f"▸ 公司 {currency} 余额增加 {amount:,.2f}"
        ]

        await update.message.reply_text("\n".join(response))

    except Exception as e:
        session.rollback()
        logger.error(f"收款处理失败: {str(e)}")
        await update.message.reply_text("❌ 操作失败")
    finally:
        Session.remove()

async def handle_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理向客户付款（直接减少公司余额，增加客户余额）"""
    session = Session()
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("❌ 参数错误！格式: /paid [客户] [金额+货币]")
            return

        customer, amount_curr = args[0], args[1]

        # 解析金额和货币
        try:
            amount = float(re.sub(r'[^\d.]', '', amount_curr))
            currency = re.search(r'[A-Za-z]{3,4}', amount_curr, re.I).group().upper()
        except (ValueError, AttributeError):
            await update.message.reply_text("❌ 金额格式错误！示例: /paid 客户A 1000USD")
            return

        # ✅ 直接更新余额
        with session.begin_nested():
            update_balance(session, customer, currency, -amount)    # 客户获得，余额增加
            update_balance(session, 'COMPANY', currency, -amount)  # 公司支付，余额减少

        # 更新 settled_out 字段
        tx = session.query(Transaction).filter(
            Transaction.customer_name == customer,
            (
                (Transaction.transaction_type == 'buy') & (Transaction.base_currency == currency) |
                (Transaction.transaction_type == 'sell') & (Transaction.quote_currency == currency)
            ),
            Transaction.status.in_(['pending', 'partial'])
        ).order_by(Transaction.timestamp.desc()).first()
        if tx:
            with session.begin_nested():
                tx.settled_out += amount  # Add to settled_out instead of setting it
                if tx.transaction_type == 'buy':
                    if tx.settled_out >= tx.amount:
                        tx.status = 'settled'
                    else:
                        tx.status = 'partial'
                elif tx.transaction_type == 'sell':
                    total_quote = tx.amount * tx.rate if tx.operator == '*' else tx.amount / tx.rate
                    if tx.settled_out >= total_quote:
                       tx.status = 'settled'
                    else:
                       tx.status = 'partial'
            session.commit()
        else:
            logger.warning(f"No matching transaction found for {customer} and {currency}")

        # 构建响应
        response = [
            f"✅ 成功向 {customer} 支付 {amount:,.2f}{currency}",
            "━━━━━━━━━━━━━━━━━━",
            f"▸ 客户 {customer} {currency} 余额增加 {amount:,.2f}",
            f"▸ 公司 {currency} 余额减少 {amount:,.2f}"
        ]

        await update.message.reply_text("\n".join(response))

    except Exception as e:
        session.rollback()
        logger.error(f"付款处理失败: {str(e)}")
        await update.message.reply_text("❌ 操作失败")
    finally:
        Session.remove()

# ================== 余额管理模块 ==================
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查询余额"""
    session = Session()
    try:
        customer = context.args[0] if context.args else 'COMPANY'
        balances = session.query(Balance).filter_by(customer_name=customer).all()
        
        if not balances:
            await update.message.reply_text(f"📭 {customer} 当前没有余额记录")
            return
            
        balance_list = "\n".join([f"▫️ {b.currency}: {b.amount:+,.2f} 💵" for b in balances])
        await update.message.reply_text(
            f"📊 *余额报告* 🏦\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 客户：{customer}\n\n"
            f"💰 当前余额：\n"
            f"{balance_list}",
            parse_mode="Markdown"
        )
    
    except Exception as e:
        logger.error(f"余额查询失败: {str(e)}")
        await update.message.reply_text("❌ 查询失败")
    finally:
        Session.remove()

async def adjust_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动调整余额"""
    session = Session()
    try:
        args = context.args
        if len(args) < 4:
            await update.message.reply_text("❌ 参数错误！格式: /adjust [客户] [货币] [±金额] [备注]")
            return

        customer, currency, amount_str, *note_parts = args
        note = ' '.join(note_parts)
        
        try:
            amount = float(amount_str)
            currency = currency.upper()
        except ValueError:
            await update.message.reply_text("❌ 金额格式错误")
            return

        # 记录调整
        adj = Adjustment(
            customer_name=customer,
            currency=currency,
            amount=amount,
            note=note
        )
        session.add(adj)
        
        # 更新余额
        update_balance(session, customer, currency, amount)
        session.commit()
        
        await update.message.reply_text(
            f"⚖️ *余额调整完成* ✅\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 客户：{customer}\n"
            f"💱 货币：{currency}\n"
            f"📈 调整量：{amount:+,.2f}\n"
            f"📝 备注：{note}"
        )
    except Exception as e:
        session.rollback()
        logger.error(f"余额调整失败: {str(e)}")
        await update.message.reply_text("❌ 调整失败")
    finally:
        Session.remove()

async def list_debts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查询欠款明细（排除公司账户）"""
    session = Session()
    try:
        customer = context.args[0] if context.args else None
        query = session.query(Balance).filter(Balance.customer_name != 'COMPANY')
        if customer:
            query = query.filter_by(customer_name=customer)
        
        balances = query.all()
        debt_report = ["📋 *欠款明细报告* ⚠️", "━━━━━━━━━━━━━━━━━━━━"]
        
        grouped = defaultdict(dict)
        for b in balances:
            grouped[b.customer_name][b.currency] = b.amount
        
        for cust, currencies in grouped.items():
            debt_report.append(f"👤 客户: {cust}")
            for curr, amt in currencies.items():
                if amt > 0.01:  # 余额为正 → 公司欠客户
                    debt_report.append(f"▫️ 公司欠客户 {amt:,.2f} {curr} 🟢")
                elif amt < -0.01:  # 余额为负 → 客户欠公司
                    debt_report.append(f"▫️ 客户欠公司 {-amt:,.2f} {curr} 🔴")
            debt_report.append("━━━━━━━━━━━━━━━━━━━━")
        
        await update.message.reply_text("\n".join(debt_report))
    except Exception as e:
        logger.error(f"欠款查询失败: {str(e)}")
        await update.message.reply_text("❌ 查询失败")
    finally:
        Session.remove()
                
# ================== 支出管理模块 ==================
async def add_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """记录公司支出"""
    session = Session()
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("❌ 参数错误！格式: /expense [金额+货币] [用途]")
            return

        amount_curr, *purpose_parts = args
        purpose = ' '.join(purpose_parts)
        
        try:
            amount = float(re.sub(r'[^\d.]', '', amount_curr))
            currency = re.search(r'[A-Z]{3,4}', amount_curr, re.I).group().upper()
        except (ValueError, AttributeError):
            await update.message.reply_text("❌ 金额格式错误！示例: /expense 100USD 办公室租金")
            return

        expense = Expense(
            amount=amount,
            currency=currency,
            purpose=purpose
        )
        session.add(expense)
        update_balance(session, 'COMPANY', currency, -amount)
        session.commit()
        
        await update.message.reply_text(
            f"💸 *支出记录已添加* ✅\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 金额：{amount:,.2f} {currency}\n"
            f"📝 用途：{purpose}\n\n"
            f"📌 公司余额已自动更新！"
        )
    except Exception as e:
        session.rollback()
        logger.error(f"支出记录失败: {str(e)}")
        await update.message.reply_text("❌ 记录失败")
    finally:
        Session.remove()

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """撤销交易并恢复初始余额"""
    session = Session()
    try:
        if not context.args:
            await update.message.reply_text("❌ 需要订单号！用法: /cancel YS000000001")
            return

        order_id = context.args[0].upper()
        tx = session.query(Transaction).filter_by(order_id=order_id).first()
        if not tx:
            await update.message.reply_text("❌ 找不到该交易")
            return

        # 计算实际交易金额（根据运算符）
        if tx.operator == '/':
            quote_amount = tx.amount / tx.rate
        else:
            quote_amount = tx.amount * tx.rate

        # 撤销初始交易影响
        if tx.transaction_type == 'buy':
            # 反向操作：
            update_balance(session, tx.customer_name, tx.base_currency, -tx.amount)  # 扣除获得的基础货币
            update_balance(session, tx.customer_name, tx.quote_currency, quote_amount)  # 恢复支付的报价货币
        else:
            update_balance(session, tx.customer_name, tx.base_currency, tx.amount)  # 恢复支付的基础货币
            update_balance(session, tx.customer_name, tx.quote_currency, -quote_amount)  # 扣除获得的报价货币

        session.delete(tx)
        session.commit()

        await update.message.reply_text(
            f"✅ 交易 {order_id} 已撤销\n"
            f"━━━━━━━━━━━━━━\n"
            f"▸ {tx.base_currency} 调整：{-tx.amount if tx.transaction_type == 'buy' else tx.amount:+,.2f}\n"
            f"▸ {tx.quote_currency} 调整：{quote_amount if tx.transaction_type == 'buy' else -quote_amount:+,.2f}"
        )

    except Exception as e:
        session.rollback()
        logger.error(f"撤销失败: {str(e)}")
        await update.message.reply_text(f"❌ 撤销失败: {str(e)}")
    finally:
        Session.remove()

async def delete_customer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """删除客户及其所有相关数据"""
    session = Session()
    try:
        args = context.args
        if not args:
            await update.message.reply_text("❌ 请输入客户名称，格式: /delete_customer [客户名]")
            return
        customer_name = args[0]

        # 删除所有相关记录（使用事务保证原子性）
        with session.begin_nested():
            # 删除客户基本信息（如果存在）
            customer = session.query(Customer).filter_by(name=customer_name).first()
            if customer:
                session.delete(customer)
                
            # 删除余额记录
            balance_count = session.query(Balance).filter_by(customer_name=customer_name).delete()
            
            # 删除交易记录
            tx_count = session.query(Transaction).filter_by(customer_name=customer_name).delete()
            
            # 删除调整记录
            adj_count = session.query(Adjustment).filter_by(customer_name=customer_name).delete()

        session.commit()

        response = (
            f"✅ 客户 *{customer_name}* 数据已清除\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"▫️ 删除余额记录：{balance_count} 条\n"
            f"▫️ 删除交易记录：{tx_count} 条\n"
            f"▫️ 删除调整记录：{adj_count} 条\n"
            f"▫️ 删除客户资料：{1 if customer else 0} 条\n\n"
            f"⚠️ 该操作不可逆，所有相关数据已从数据库中清除"
        )
        await update.message.reply_text(response, parse_mode="Markdown")

    except Exception as e:
        session.rollback()
        logger.error(f"删除客户失败: {str(e)}", exc_info=True)
        await update.message.reply_text(
            "❌ 删除操作失败！\n"
            "⚠️ 错误详情请查看服务器日志"
        )
    finally:
        Session.remove()

# ================== 支出管理模块（续） ==================
async def list_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查询支出记录"""
    session = Session()
    try:
        expenses = session.query(Expense).order_by(Expense.timestamp.desc()).all()
        if not expenses:
            await update.message.reply_text("📝 当前无支出记录")
            return

        report = ["📋 公司支出记录", "━━━━━━━━━━━━━━━"]
        for exp in expenses:
            report.append(
                f"▫️ {exp.timestamp.strftime('%Y-%m-%d %H:%M')}\n"
                f"金额: {exp.amount:,.2f} {exp.currency}\n"
                f"用途: {exp.purpose}\n"
                "━━━━━━━━━━━━━━━"
            )
        
        # 分页发送防止消息过长
        full_report = "\n".join(report)
        for i in range(0, len(full_report), 4000):
            await update.message.reply_text(full_report[i:i+4000])
    except Exception as e:
        logger.error(f"支出查询失败: {str(e)}")
        await update.message.reply_text("❌ 查询失败")
    finally:
        Session.remove()

# ================== 报表生成模块 ==================
async def pnl_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """生成精准的货币独立盈亏报告（针对订单计算盈亏）"""
    session = Session()
    try:
        # 解析参数
        args = context.args or []
        excel_mode = 'excel' in args
        date_args = [a for a in args if a != 'excel']
        
        # 解析日期范围
        if date_args:
            try:
                start_date, end_date = parse_date_range(' '.join(date_args))
            except ValueError as e:
                await update.message.reply_text(f"❌ {str(e)}")
                return
        else:
            now = datetime.now()
            start_date = now.replace(day=1, hour=0, minute=0, second=0)
            end_date = now.replace(day=calendar.monthrange(now.year, now.month)[1], 
                                hour=23, minute=59, second=59)

        # 获取交易记录和支出记录
        txs = session.query(Transaction).filter(
            Transaction.timestamp.between(start_date, end_date)
        ).all()
        
        expenses = session.query(Expense).filter(
            Expense.timestamp.between(start_date, end_date)
        ).all()

        # 初始化货币报告
        currency_report = defaultdict(lambda: {
            'actual_income': 0.0,  # 实际收入（已结算）
            'actual_expense': 0.0,  # 实际支出（已结算）
            'pending_income': 0.0,  # 应收未收
            'pending_expense': 0.0,  # 应付未付
            'credit_balance': 0.0,  # 客户多付的信用余额
            'total_income': 0.0,    # 总应收款
            'total_expense': 0.0,   # 总应付款
            'expense': 0.0          # 支出
        })

        # 处理交易记录
        for tx in txs:
            # 根据运算符计算报价货币金额
            if tx.operator == '/':
                total_quote = tx.amount / tx.rate
            else:
                total_quote = tx.amount * tx.rate

            if tx.transaction_type == 'buy':
                # 买入交易：客户支付报价货币，获得基础货币
                currency_report[tx.quote_currency]['total_income'] += total_quote  # 总应收款
                currency_report[tx.quote_currency]['actual_income'] += tx.settled_in  # 已收款
                currency_report[tx.quote_currency]['pending_income'] += total_quote - tx.settled_in  # 应收未收
                currency_report[tx.base_currency]['total_expense'] += tx.amount  # 总应付款
                currency_report[tx.base_currency]['actual_expense'] += tx.settled_out  # 已付款
                currency_report[tx.base_currency]['pending_expense'] += tx.amount - tx.settled_out  # 应付未付
            else:
                # 卖出交易：客户支付基础货币，获得报价货币
                currency_report[tx.base_currency]['total_income'] += tx.amount  # 总应收款
                currency_report[tx.base_currency]['actual_income'] += tx.settled_in  # 已收款
                currency_report[tx.base_currency]['pending_income'] += tx.amount - tx.settled_in  # 应收未收
                currency_report[tx.quote_currency]['total_expense'] += total_quote  # 总应付款
                currency_report[tx.quote_currency]['actual_expense'] += tx.settled_out  # 已付款
                currency_report[tx.quote_currency]['pending_expense'] += total_quote - tx.settled_out  # 应付未付

        # 处理支出记录
        for exp in expenses:
            currency_report[exp.currency]['expense'] += exp.amount
            currency_report[exp.currency]['actual_expense'] += exp.amount

        # 计算客户多付的信用余额
        for currency, data in currency_report.items():
            # 信用余额 = 已收款 - 总应收款
            data['credit_balance'] = max(0, data['actual_income'] - data['total_income'])

        # ================== Excel报表生成 ==================
        if excel_mode:
            # 交易明细
            tx_data = []
            for tx in txs:
                if tx.operator == '/':
                    total_quote = tx.amount / tx.rate
                else:
                    total_quote = tx.amount * tx.rate

                # 结算金额计算
                settled_base = tx.settled_out if tx.transaction_type == 'buy' else tx.settled_in
                settled_quote = tx.settled_in if tx.transaction_type == 'buy' else tx.settled_out
    
                # 计算双货币进度
                base_progress = settled_base / tx.amount if tx.amount != 0 else 0
                quote_progress = settled_quote / total_quote if total_quote != 0 else 0
                min_progress = min(base_progress, quote_progress)
    
                # 状态判断（取整后判断）
                base_done = int(settled_base) >= int(tx.amount)
                quote_done = int(settled_quote) >= int(total_quote)
                status = "已完成" if base_done and quote_done else "进行中"

                tx_data.append({
                    "订单号": tx.order_id,
                    "客户名称": tx.customer_name,
                    "交易类型": '买入' if tx.transaction_type == 'buy' else '卖出',
                    "基础货币总额": f"{tx.amount:,.2f} {tx.base_currency}",
                    "报价货币总额": f"{total_quote:,.2f} {tx.quote_currency}",
                    "已结基础货币": f"{settled_base:,.2f} {tx.base_currency}",
                    "已结报价货币": f"{settled_quote:,.2f} {tx.quote_currency}",  # 新增结算金额
                    "基础货币进度": f"{base_progress:.1%}",
                    "报价货币进度": f"{quote_progress:.1%}",
                    "状态": status
                })

            # 货币汇总
            currency_data = []
            for curr, data in currency_report.items():
                currency_data.append({
                    "货币": curr,
                    "实际收入": f"{data['actual_income']:,.2f}",
                    "实际支出": f"{data['actual_expense']:,.2f}",
                    "应收未收": f"{data['pending_income']:,.2f}",
                    "应付未付": f"{data['pending_expense']:,.2f}",
                    "信用余额": f"{data['credit_balance']:,.2f}",
                    "净盈亏": f"{data['actual_income'] - data['actual_expense']:,.2f}"
                })

            # 支出记录
            expense_data = [{
                "日期": exp.timestamp.strftime('%Y-%m-%d'),
                "金额": f"{exp.amount:,.2f}",
                "货币": exp.currency,
                "用途": exp.purpose
            } for exp in expenses]

            # 生成Excel
            df_dict = {
                "交易明细": pd.DataFrame(tx_data),
                "货币汇总": pd.DataFrame(currency_data),
                "支出记录": pd.DataFrame(expense_data)
            }
            
            excel_buffer = generate_excel_buffer(df_dict, ["交易明细", "货币汇总", "支出记录"])
            await update.message.reply_document(
                document=excel_buffer,
                filename=f"盈亏报告_{start_date.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}.xlsx",
                caption="📊 包含货币独立盈亏的Excel报告"
            )
            return
        
        # ================== 生成文本报告 ==================
        report = [
            f"📊 *盈亏报告* ({start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')})",
            f"▫️ 有效交易：{len(txs)}笔 | 支出记录：{len(expenses)}笔",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ]
        
        for curr, data in currency_report.items():
            profit = data['actual_income'] - data['actual_expense']
            report.append(
                f"🔘 *{curr}* 货币\n"
                f"▸ 实际收入：{data['actual_income']:+,.2f}\n"
                f"▸ 实际支出：{data['actual_expense']:+,.2f}\n"
                f"▸ 应收未收：{data['pending_income']:,.2f}\n"
                f"▸ 应付未付：{data['pending_expense']:,.2f}\n"
                f"▸ 信用余额：{data['credit_balance']:,.2f}\n"
                f"🏁 净盈亏：{profit:+,.2f}\n"
                "━━━━━━━━━━━━━━━━━━"
            )
            
        await update.message.reply_text("\n".join(report))

    except Exception as e:
        logger.error(f"盈亏报告生成失败: {str(e)}", exc_info=True)
        await update.message.reply_text("❌ 报告生成失败，请检查日志")
    finally:
        Session.remove()

async def generate_detailed_report(update: Update, context: ContextTypes.DEFAULT_TYPE, period: str):
    session = Session()
    try:
        args = context.args or []
        excel_mode = 'excel' in args
        date_args = [a for a in args if a != 'excel']
        
        # 解析日期范围（增强容错）
        if date_args:
            try:
                if '-' in ' '.join(date_args):
                    start_date, end_date = parse_date_range(' '.join(date_args))
                else:
                    single_date = datetime.strptime(' '.join(date_args), '%d/%m/%Y')
                    start_date = single_date.replace(hour=0, minute=0, second=0)
                    end_date = single_date.replace(hour=23, minute=59, second=59)
            except ValueError as e:
                await update.message.reply_text(f"❌ 日期格式错误，请使用 DD/MM/YYYY 或 DD/MM/YYYY-DD/MM/YYYY")
                return
        else:
            now = datetime.now()
            start_date = now.replace(day=1, hour=0, minute=0, second=0)
            end_date = now.replace(day=calendar.monthrange(now.year, now.month)[1], 
                                hour=23, minute=59, second=59)

        # 获取交易记录和客户信用余额
        txs = session.query(Transaction).filter(
            Transaction.timestamp.between(start_date, end_date)
        ).all()
        
        # 获取所有客户的信用余额
        credit_balances = session.query(
            Balance.customer_name,
            Balance.currency,
            func.sum(Balance.amount).label('credit')
        ).filter(Balance.amount > 0).group_by(Balance.customer_name, Balance.currency).all()

        # Excel生成修正
        if excel_mode:
            tx_data = []
            for tx in txs:
                try:
                    # 计算应付总额和信用余额
                    if tx.operator == '/':
                        total_quote = tx.amount / tx.rate
                    else:
                        total_quote = tx.amount * tx.rate
                        
                    # 获取该客户的信用余额
                    credit = next(
                        (cb.credit for cb in credit_balances 
                         if cb.customer_name == tx.customer_name 
                         and cb.currency == tx.quote_currency),
                        0.0
                    )

                    # 根据交易类型确定结算逻辑
                    if tx.transaction_type == 'buy':
                        # 买入交易：客户应支付报价货币
                        required = total_quote
                        settled = tx.settled_in
                        credit_used = min(credit, required - settled)
                    else:
                        # 卖出交易：客户应支付基础货币
                        required = tx.amount
                        settled = tx.settled_in
                        credit_used = min(credit, required - settled)

                    # 计算实际需要支付的金额
                    actual_payment = settled + credit_used
                    remaining = required - actual_payment
                    progress = actual_payment / required if required != 0 else 0

                    # 判断状态
                    if tx.transaction_type == 'buy':
                        # 买入交易判断逻辑
                        base_done = int(tx.settled_out) >= int(tx.amount)  # 公司支付的基础货币
                        quote_done = int(tx.settled_in) >= int(total_quote)  # 客户支付的报价货币
                    else:
                        # 卖出交易判断逻辑
                        base_done = int(tx.settled_in) >= int(tx.amount)    # 客户支付的基础货币
                        quote_done = int(tx.settled_out) >= int(total_quote) # 公司支付的报价货币

                    status = "已完成" if base_done and quote_done else "进行中"    

                    if tx.transaction_type == 'buy':
                        settled_base = tx.settled_out  # 公司已支付的基础货币
                        settled_quote = tx.settled_in  # 客户已支付的报价货币
                    else:
                        settled_base = tx.settled_in   # 客户已支付的基础货币
                        settled_quote = tx.settled_out # 公司已支付的报价货币                        

                    record = {
                        "订单号": tx.order_id,
                        "客户名称": tx.customer_name,
                        "交易类型": '买入' if tx.transaction_type == 'buy' else '卖出',
                        "基础货币总额": f"{tx.amount:,.2f} {tx.base_currency}",
                        "报价货币总额": f"{total_quote:,.2f} {tx.quote_currency}",
                        "已结基础货币": f"{settled_base:,.2f} {tx.base_currency}",
                        "已结报价货币": f"{settled_quote:,.2f} {tx.quote_currency}", 
                        "基础货币进度": f"{(tx.settled_out / tx.amount * 100):.1f}%" if tx.transaction_type == 'buy' else f"{(tx.settled_in / tx.amount * 100):.1f}%",
                        "报价货币进度": f"{(tx.settled_in / total_quote * 100):.1f}%" if tx.transaction_type == 'buy' else f"{(tx.settled_out / total_quote * 100):.1f}%",
                        "状态": status  # 使用新的状态判断
                    }
                    tx_data.append(record)
                except Exception as e:
                    logger.error(f"处理交易 {tx.order_id} 失败: {str(e)}")
                    continue
            
            if not tx_data:
                await update.message.reply_text("⚠️ 该时间段内无交易记录")
                return

            # 生成信用余额表
            credit_data = [{
                "客户名称": cb.customer_name,
                "货币": cb.currency,
                "信用余额": f"{cb.credit:,.2f}"
            } for cb in credit_balances]

            df_dict = {
                "交易明细": pd.DataFrame(tx_data),
                "信用余额": pd.DataFrame(credit_data)
            }
            
            excel_buffer = generate_excel_buffer(df_dict, ["交易明细", "信用余额"])
            await update.message.reply_document(
                document=excel_buffer,
                filename=f"交易明细_{start_date.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}.xlsx",
                caption="📊 包含信用对冲的Excel交易明细"
            )
            return

        # 文本报告生成
        report = [
            f"📋 交易结算明细报告 ({start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
            f"总交易数: {len(txs)}",
            "━━━━━━━━━━━━━━━━━━"
        ]
        
        for tx in txs:
            # 计算应付总额
            if tx.operator == '/':
                total_quote = tx.amount / tx.rate
            else:
                total_quote = tx.amount * tx.rate

            # 获取信用余额
            credit = next(
                (cb.credit for cb in credit_balances 
                 if cb.customer_name == tx.customer_name 
                 and cb.currency == (tx.quote_currency if tx.transaction_type == 'buy' else tx.base_currency)),
                0.0
            )

            # 状态判断
            required = total_quote if tx.transaction_type == 'buy' else tx.amount
            settled = tx.settled_in
            remaining = required - settled - min(credit, required - settled)
            
            base_settled = tx.settled_in if tx.transaction_type == 'sell' else tx.settled_out
            quote_settled = tx.settled_out if tx.transaction_type == 'sell' else tx.settled_in

            status = "✅ 已完成" if abs(remaining) <= 1.00 else f"🟡 部分结算 (剩余: {remaining:,.2f})"

            report.append(
                f"📌 {tx.timestamp.strftime('%d/%m %H:%M')} {tx.order_id}\n"
                f"{tx.customer_name} {'买入' if tx.transaction_type == 'buy' else '卖出'} "
                f"{tx.amount:,.2f} {tx.base_currency} @ {tx.rate:.4f}\n"
                f"▸ 应付基础货币: {tx.amount:,.2f} {tx.base_currency} (已结: {base_settled:,.2f})\n"
                f"▸ 应付报价货币: {total_quote:,.2f} {tx.quote_currency} (已结: {quote_settled:,.2f})\n"
                f"▸ 状态: {'✅ 已完成' if int(base_settled) >= int(tx.amount) and int(quote_settled) >= int(total_quote) else '🟡 进行中'}"
                "━━━━━━━━━━━━━━━━━━"
            )
        
        await update.message.reply_text("\n".join(report))
        
    except Exception as e:
        logger.error(f"交易报表生成失败: {str(e)}")
        await update.message.reply_text("❌ 生成失败")
    finally:
        Session.remove()
                
async def customer_statement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """生成客户对账单，支持Excel格式"""
    session = Session()
    try:
        args = context.args or []
        if not args:
            await update.message.reply_text("❌ 需要客户名称！格式: /creport [客户名] [日期范围] [excel]")
            return

        excel_mode = 'excel' in args
        clean_args = [a for a in args if a != 'excel']
        customer = clean_args[0]
        date_args = clean_args[1:]

        # 解析日期范围
        if date_args:
            try:
                start_date, end_date = parse_date_range(' '.join(date_args))
            except ValueError as e:
                await update.message.reply_text(f"❌ {str(e)}")
                return
        else:
            # 如果没有提供日期范围，默认使用当前月份的范围
            now = datetime.now()
            start_date = now.replace(day=1, hour=0, minute=0, second=0)
            end_date = now.replace(day=calendar.monthrange(now.year, now.month)[1], 
                                hour=23, minute=59, second=59)

        # 获取数据
        balances = session.query(Balance).filter_by(customer_name=customer).all()
        txs = session.query(Transaction).filter(
            Transaction.customer_name == customer,
            Transaction.timestamp.between(start_date, end_date)
        ).all()
        
        adjs = session.query(Adjustment).filter(
            Adjustment.customer_name == customer,
            Adjustment.timestamp.between(start_date, end_date)
        ).all()

        # 生成Excel报表
        # 生成Excel报表
        if excel_mode:
            # 交易明细
            tx_data = []
            for tx in txs:
                if tx.operator == '/':
                    total_quote = tx.amount / tx.rate
                else:
                    total_quote = tx.amount * tx.rate
                # ==== 关键修复1：结算金额与进度计算 ====
                if tx.transaction_type == 'buy':
                    # 买入交易：
                    # - 基础货币（公司支付给客户）：settled_out
                    # - 报价货币（客户支付给公司）：settled_in
                    settled_base = tx.settled_out
                    settled_quote = tx.settled_in
                    base_progress = settled_base / tx.amount if tx.amount != 0 else 0
                    quote_progress = settled_quote / total_quote if total_quote != 0 else 0
                else:
                    # 卖出交易：
                    # - 基础货币（客户支付给公司）：settled_in
                    # - 报价货币（公司支付给客户）：settled_out
                    settled_base = tx.settled_in
                    settled_quote = tx.settled_out
                    base_progress = settled_base / tx.amount if tx.amount != 0 else 0
                    quote_progress = settled_quote / total_quote if total_quote != 0 else 0
                # ==== 关键修复2：状态判断 ====
                base_done = int(settled_base) >= int(tx.amount)
                quote_done = int(settled_quote) >= int(total_quote)
                status = "已完成" if base_done and quote_done else "进行中"
                tx_data.append({
                    "日期": tx.timestamp.strftime('%Y-%m-%d'),
                    "订单号": tx.order_id,
                    "交易类型": '买入' if tx.transaction_type == 'buy' else '卖出',
                    "基础货币总额": f"{tx.amount:,.2f} {tx.base_currency}",
                    "报价货币总额": f"{total_quote:,.2f} {tx.quote_currency}",
                    "已结基础货币": f"{settled_base:,.2f} {tx.base_currency}",
                    "已结报价货币": f"{settled_quote:,.2f} {tx.quote_currency}",
                    "进度": f"{min(base_progress, quote_progress):.1%}",
                    "状态": status
                })
    
            # 余额数据
            balance_data = [{
                "货币": b.currency,
                "余额": f"{b.amount:,.2f}"
            } for b in balances]
    
            # 将余额数据添加到交易明细中
            for balance in balance_data:
                tx_data.append({
                    "日期": "",
                    "订单号": "",
                    "交易类型": "",
                    "基础货币总额": "",
                    "报价货币总额": "",
                    "已结基础货币": "",
                    "已结报价货币": "",
                    "进度": "",
                    "状态": "",
                    "货币余额": f"{balance['货币']}: {balance['余额']}"
                })
    
            # 调整记录
            adj_data = [{
                "日期": adj.timestamp.strftime('%Y-%m-%d'),
                "金额": f"{adj.amount:+,.2f}",
                "货币": adj.currency,
                "备注": adj.note
            } for adj in adjs]
    
            # 生成Excel
            df_dict = {
                "交易明细与余额": pd.DataFrame(tx_data),
                "调整记录": pd.DataFrame(adj_data)
            }
    
            excel_buffer = generate_excel_buffer(df_dict, ["交易明细与余额", "调整记录"])
            await update.message.reply_document(
                document=excel_buffer,
                filename=f"客户对账单_{customer}_{start_date.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}.xlsx",
                caption=f"📊 {customer} Excel对账单"
            )
            return

        # 生成文本报告
        report = [
            f"📑 客户对账单 - {customer}",
            f"日期范围: {start_date.strftime('%d/%m/%Y')} - {end_date.strftime('%d/%m/%Y')}",
            f"生成时间: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
            "━━━━━━━━━━━━━━━━━━"
        ]

        # 余额部分
        balance_section = ["📊 当前余额:"]
        if balances:
            balance_section += [f"• {b.currency}: {b.amount:+,.2f}" for b in balances]
        report.extend(balance_section)

        # 交易记录
        tx_section = ["\n💵 交易记录:"]
        if txs:
            for tx in txs:
                if tx.operator == '/':
                    total_quote = tx.amount / tx.rate
                else:
                    total_quote = tx.amount * tx.rate

                # ==== 关键修复3：文本报表的结算金额与进度 ====
                if tx.transaction_type == 'buy':
                    settled_base = tx.settled_out
                    settled_quote = tx.settled_in
                else:
                    settled_base = tx.settled_in
                    settled_quote = tx.settled_out

                base_progress = settled_base / tx.amount if tx.amount != 0 else 0
                quote_progress = settled_quote / total_quote if total_quote != 0 else 0
                base_done = int(settled_base) >= int(tx.amount)
                quote_done = int(settled_quote) >= int(total_quote)
                status = "已完成" if base_done and quote_done else "进行中"

                tx_section.append(
                    f"▫️ {tx.timestamp.strftime('%d/%m %H:%M')} {tx.order_id}\n"
                    f"{'买入' if tx.transaction_type == 'buy' else '卖出'} "
                    f"{tx.amount:,.2f} {tx.base_currency} @ {tx.rate:.4f}\n"
                    f"├─ 已结基础货币: {settled_base:,.2f}/{tx.amount:,.2f} {tx.base_currency} ({base_progress:.1%})\n"
                    f"├─ 已结报价货币: {settled_quote:,.2f}/{total_quote:,.2f} {tx.quote_currency} ({quote_progress:.1%})\n"
                    f"└─ 状态: {status}"
                )
        else:
            tx_section.append("无交易记录")
        report.extend(tx_section)

        # 调整记录
        adj_section = ["\n📝 调整记录:"]
        if adjs:
            for adj in adjs:
                adj_section.append(
                    f"{adj.timestamp.strftime('%d/%m %H:%M')}\n"
                    f"{adj.currency}: {adj.amount:+,.2f} - {adj.note}"
                )
        else:
            adj_section.append("无调整记录")
        report.extend(adj_section)

        # 发送报告
        full_report = "\n".join(report)
        for i in range(0, len(full_report), 4000):
            await update.message.reply_text(full_report[i:i+4000])
    except Exception as e:
        logger.error(f"对账单生成失败: {str(e)}")
        await update.message.reply_text("❌ 生成失败")
    finally:
        Session.remove()

# ================== 机器人命令注册 ==================
def main():
    run_migrations()  # 新增此行
    setup_logging()
    application = ApplicationBuilder().token("7706817515:AAHuQL4myZYqg6HMzejc82RDJTvkMCI8JXo").build()
    
    handlers = [
        CommandHandler('start', lambda u,c: u.message.reply_text(
            "🤖 *阳陞国际会计机器人*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📚 可用命令：\n\n"
            "💼 *账户管理*\n"
            "▫️ `/balance [客户]` 查询余额 📊\n"
            "▫️ `/debts [客户]` 查看欠款明细 🧾\n"
            "▫️ `/adjust [客户] [货币] [±金额] [备注]` 调整余额 ⚖️\n\n"
            "▫️ `/delete_customer [客户名]` 删除客户及其所有数据 ⚠️\n\n"  # 
            "💸 *交易操作*\n"
            "▫️ `客户A 买 10000USD /4.42 MYR` 创建交易\n"
            "▫️ `/received [客户] [金额+货币]` 登记客户付款\n"
            "▫️ `/paid [客户] [金额+货币]` 登记向客户付款\n"
            "▫️ `/cancel [订单号]` 撤销未结算交易\n\n"
            "📈 *财务报告*\n"
            "▫️ `/pnl [日期范围] [excel]` 盈亏报告 📉\n"
            "▫️ `/report [日期范围] [excel]` 交易明细 📋\n"
            "▫️ `/creport [客户] [日期范围] [excel]` 客户对账单 📑\n"
            "▫️ `/expense [金额+货币] [用途]` 记录支出 💸\n"
            "▫️ `/expenses` 支出记录 🧮\n\n"
            "💡 *使用提示*\n"
            "🔸 日期格式：`DD/MM/YYYY-DD/MM/YYYY`\n"
            "🔸 添加 `excel` 参数获取表格文件 📤\n"
            "🔸 示例：`/pnl 01/01/2025-31/03/2025 excel`"
        )),
        CommandHandler('balance', balance),
        CommandHandler('debts', list_debts),
        CommandHandler('adjust', adjust_balance),
        CommandHandler('received', handle_received),
        CommandHandler('paid', handle_paid),
        CommandHandler('cancel', cancel_order),
        CommandHandler('pnl', pnl_report),
        CommandHandler('expense', add_expense),
        CommandHandler('expenses', list_expenses),
        CommandHandler('creport', customer_statement),
        CommandHandler('report', lambda u,c: generate_detailed_report(u, c, 'daily')),
        CommandHandler('delete_customer', delete_customer),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_transaction)
    ]
    
    application.add_handlers(handlers)
    logger.info("机器人启动成功")
    application.run_polling()

if __name__ == '__main__':
    main()