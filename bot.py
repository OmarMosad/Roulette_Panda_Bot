import os
from dotenv import load_dotenv
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    PreCheckoutQueryHandler,
    ConversationHandler,
    filters
)
import random
from datetime import datetime, timedelta
import re
import asyncpg
import asyncio
import platform

# تحميل متغيرات البيئة
load_dotenv()

# تكوين التسجيل
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# بيانات البوت من متغيرات البيئة
TOKEN = os.getenv('BOT_TOKEN')
CHANNEL = f"@{os.getenv('CHANNEL_USERNAME')}"
ADMINS = [int(id) for id in os.getenv('ADMIN_IDS').split(',')] if os.getenv('ADMIN_IDS') else []
DATABASE_URL = os.getenv('DATABASE_URL')
SUPPORT_USERNAME = "@OMAR_M_SHEHATA"

# حالات المحادثة
(START, MAIN_MENU, CREATE_ROULETTE, ADD_CHANNEL, PAYMENT, 
 WAITING_FOR_TEXT, WAITING_FOR_WINNERS, ADMIN_MENU, LINK_CHANNEL) = range(9)

# أسعار الخدمات
PRICES = {
    'premium_month': 100,
    'add_channel_once': 7,
    'donate': 15
}

STARS_CURRENCY = "XTR"

async def init_db():
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            stars INTEGER DEFAULT 0,
            points INTEGER DEFAULT 0,
            is_premium BOOLEAN DEFAULT FALSE,
            premium_expiry TIMESTAMP,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now(),
            linked_channel TEXT
        );
        
        CREATE TABLE IF NOT EXISTS roulettes (
            id SERIAL PRIMARY KEY,
            creator_id BIGINT,
            message TEXT,
            channel_id TEXT,
            condition_channel_id TEXT,
            winner_count INTEGER,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT now(),
            message_id BIGINT,
            chat_id BIGINT
        );
        
        CREATE TABLE IF NOT EXISTS participants (
            id SERIAL PRIMARY KEY,
            roulette_id INTEGER REFERENCES roulettes(id) ON DELETE CASCADE,
            user_id BIGINT,
            username TEXT,
            full_name TEXT,
            joined_at TIMESTAMP DEFAULT now()
        );
        
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            payment_type TEXT,
            amount INTEGER,
            is_completed BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT now(),
            completed_at TIMESTAMP
        );
        
        CREATE TABLE IF NOT EXISTS donations (
            id SERIAL PRIMARY KEY,
            donor_id BIGINT,
            amount INTEGER,
            donation_date TIMESTAMP DEFAULT now()
        );
        
        CREATE TABLE IF NOT EXISTS point_transactions (
            id SERIAL PRIMARY KEY,
            admin_id BIGINT,
            user_id BIGINT,
            points INTEGER,
            transaction_date TIMESTAMP DEFAULT now(),
            notes TEXT
        );
        """)
    return pool

async def check_user_payment_status(user_id: int, pool) -> dict:
    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT is_premium, premium_expiry, stars, points, linked_channel
            FROM users WHERE telegram_id = $1
        """, user_id)
        
        if not user:
            await conn.execute("INSERT INTO users (telegram_id) VALUES ($1)", user_id)
            return {
                'is_premium': False,
                'premium_expiry': None,
                'stars': 0,
                'points': 0,
                'linked_channel': None
            }
        
        # نحولها لقاموس علشان نقدر نعدل عليها
        user_dict = dict(user)

        # التحقق من انتهاء الاشتراك
        if (
            user_dict['is_premium'] and 
            user_dict['premium_expiry'] and 
            user_dict['premium_expiry'] < datetime.now()
        ):
            # نحدث القاعدة ونعدل القيم في القاموس
            await conn.execute("""
                UPDATE users 
                SET is_premium = FALSE, premium_expiry = NULL 
                WHERE telegram_id = $1
            """, user_id)
            user_dict['is_premium'] = False
            user_dict['premium_expiry'] = None

        return user_dict


        # تحقق من انتهاء البريميوم
        if user_dict['is_premium'] and user_dict['premium_expiry'] and user_dict['premium_expiry'] < datetime.now():
            # الاشتراك خلص، نحدث البيانات
            await conn.execute("""
                UPDATE users 
                SET is_premium = FALSE, premium_expiry = NULL 
                WHERE telegram_id = $1
            """, user_id)
            user_dict['is_premium'] = False
            user_dict['premium_expiry'] = None

        return user_dict


async def process_payment(user_id: int, payment_type: str, pool, use_points: bool = False) -> bool:
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT stars, points FROM users WHERE telegram_id = $1", user_id)
        if not user:
            return False
            
        required_amount = PRICES.get(payment_type, 0)
        balance = user['points'] if use_points else user['stars']
        
        if balance < required_amount:
            return False
            
        if use_points:
            await conn.execute("""
                UPDATE users SET points = points - $1 WHERE telegram_id = $2
            """, required_amount, user_id)
        else:
            await conn.execute("""
                UPDATE users SET stars = stars - $1 WHERE telegram_id = $2
            """, required_amount, user_id)
        
        if payment_type == 'premium_month':
            expiry_date = datetime.now() + timedelta(days=30)
            await conn.execute("""
                UPDATE users 
                SET is_premium = TRUE, premium_expiry = $1 
                WHERE telegram_id = $2
            """, expiry_date, user_id)
        
        await conn.execute("""
            INSERT INTO payments (user_id, payment_type, amount, is_completed, completed_at)
            VALUES ($1, $2, $3, TRUE, now())
        """, user_id, payment_type, required_amount)
        
        return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    user_id = user.id
    
    if user_id in ADMINS:
        await show_admin_menu(update, context)
        return ADMIN_MENU
    
    try:
        member = await context.bot.get_chat_member(CHANNEL, user_id)
        if member.status not in ['member', 'administrator', 'creator']:
            await show_channel_subscription(update, context)
            return START
    except Exception as e:
        logger.error(f"Error checking channel membership: {e}")
        await show_channel_subscription(update, context)
        return START
    
    await show_main_menu(update, context)
    return MAIN_MENU

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("إضافة نقاط لمستخدم", callback_data='add_points')],
        [InlineKeyboardButton("القائمة الرئيسية", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = "مرحبًا بك في لوحة التحكم الإدارية:"
    
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=reply_markup
            )
        except Exception as e:
            if "Message is not modified" in str(e):
                await update.callback_query.answer()
            else:
                logger.error(f"Error in show_admin_menu: {e}")
                await update.callback_query.answer("حدث خطأ أثناء التحميل", show_alert=True)
    else:
        await update.message.reply_text(
            text=text,
            reply_markup=reply_markup
        )

async def admin_add_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text="أرسل معرف المستخدم وعدد النقاط التي تريد إضافتها بالصيغة التالية:\n\n"
             "user_id:points\n\n"
             "مثال:\n123456789:100",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data='admin_menu')]])
    )
    return ADMIN_MENU

async def admin_handle_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text
    pool = context.bot_data.get('pool')
    
    try:
        if ':' in text:
            target_user_id, points = text.split(':')
            target_user_id = int(target_user_id.strip())
            points = int(points.strip())
            
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE users SET points = points + $1 WHERE telegram_id = $2
                """, points, target_user_id)
                
                await conn.execute("""
                    INSERT INTO point_transactions (admin_id, user_id, points, notes)
                    VALUES ($1, $2, $3, $4)
                """, user_id, target_user_id, points, "إضافة نقاط من قبل المشرف")
                
            await update.message.reply_text(f"تم إضافة {points} نقطة للمستخدم {target_user_id} بنجاح!")
        else:
            await update.message.reply_text("الصيغة غير صحيحة. يرجى استخدام الصيغة: user_id:points")
            
    except Exception as e:
        logger.error(f"Error in admin_handle_points: {e}")
        await update.message.reply_text("حدث خطأ أثناء معالجة طلبك. يرجى المحاولة مرة أخرى.")
    
    await show_admin_menu(update, context)
    return ADMIN_MENU

async def show_channel_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("قناتنا", url=f"https://t.me/{CHANNEL[1:]}")],
        [InlineKeyboardButton("لقد اشتركت في القناة", callback_data='subscribed')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "السلام عليكم ورحمة الله وبركاته\n"
        "مرحبًا بك في باندا روليت!\n"
        "يجب الاشتراك في قناتنا أولاً للمتابعة:",
        reply_markup=reply_markup
    )

async def subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    try:
        member = await context.bot.get_chat_member(CHANNEL, user_id)
        if member.status not in ['member', 'administrator', 'creator']:
            await query.answer("لم يتم العثور على اشتراكك. يرجى الاشتراك أولاً!", show_alert=True)
            return START
    except Exception as e:
        logger.error(f"Error rechecking channel membership: {e}")
        await query.answer("حدث خطأ أثناء التحقق من اشتراكك. حاول مرة أخرى!", show_alert=True)
        return START
    
    if user_id in ADMINS:
        await show_admin_menu(update, context)
        return ADMIN_MENU
    else:
        await show_main_menu(update, context)
        return MAIN_MENU

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pool = context.bot_data.get('pool')
    user_status = await check_user_payment_status(user_id, pool)
    
    keyboard = [
        [InlineKeyboardButton("إنشاء الروليت", callback_data='create_roulette')],
        [
            InlineKeyboardButton("ربط القناة", callback_data='link_channel'),
            InlineKeyboardButton("فصل القناة", callback_data='unlink_channel')
        ],
        [
            InlineKeyboardButton("🔔 ذكرني إذا فزت 💌", callback_data='remind_me'),
            InlineKeyboardButton("شاركنا الرحلة 💖", callback_data='donate_menu')
        ],
        [InlineKeyboardButton("🛠 الدعم الفني", callback_data='support')],
        [InlineKeyboardButton(f"رصيدك: {user_status['points']} نقطة", callback_data='balance')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = "مرحبًا بك في القائمة الرئيسية لباندا روليت:"
    
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=reply_markup
            )
        except Exception as e:
            if "Message is not modified" in str(e):
                await update.callback_query.answer()
            else:
                logger.error(f"Error in show_main_menu: {e}")
                await update.callback_query.answer("حدث خطأ أثناء التحميل", show_alert=True)
    else:
        await update.message.reply_text(
            text=text,
            reply_markup=reply_markup
        )

async def create_roulette(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    pool = context.bot_data.get('pool')
    
    # التحقق من وجود قناة مربوطة
    user_status = await check_user_payment_status(user_id, pool)
    if not user_status['linked_channel']:
        await query.edit_message_text(
            text="⚠️ يجب ربط قناة أولاً قبل إنشاء السحب\n\n"
                 "يرجى ربط قناة من القائمة الرئيسية ثم المحاولة مرة أخرى",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ربط قناة", callback_data='link_channel')]])
        )
        return MAIN_MENU
    
    instructions = (
        "أرسل كليشة السحب\n\n"
        "1 - للتشويش: <tg-spoiler>مثال</tg-spoiler>\n"
        "2 - للتعريض: <b>مثال</b>\n"
        "3 - للنص المائل: <i>مثال</i>\n"
        "4 - للمقتبس: <blockquote>مثال</blockquote>\n\n"
        "رجاءً عدم إرسال أي روابط"
    )
    
    keyboard = [[InlineKeyboardButton("رجوع", callback_data='back_to_main')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=instructions,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )
    
    return WAITING_FOR_TEXT

async def handle_roulette_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    roulette_text = update.message.text
    
    context.user_data['roulette_text'] = roulette_text
    
    keyboard = [
        [InlineKeyboardButton("إضافة قناة الشرط", callback_data='add_channel')],
        [InlineKeyboardButton("تخطي", callback_data='skip_channel')],
        [InlineKeyboardButton("رجوع", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        text="هل تريد إضافة قناة شرط؟\n"
             "عند إضافة قناة شرط لن يتمكن أحد من المشاركة في السحب قبل الإنضمام للقناة",
        reply_markup=reply_markup
    )
    
    return ADD_CHANNEL

# ... (بقية الاستيرادات والمتغيرات كما هي)

# تعديل دالة handle_payment
# هذا هو الشكل الصحيح الذي يجب أن يبقى
async def handle_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    payment_type = query.data
    pool = context.bot_data.get('pool')
    
    if not pool:
        await query.answer("حدث خطأ في النظام. يرجى المحاولة لاحقًا.", show_alert=True)
        return MAIN_MENU

    payment_map = {
        'upgrade_month': 'premium_month',
        'upgrade_once': 'add_channel_once',
        'upgrade_month_points': 'premium_month',
        'upgrade_once_points': 'add_channel_once'
    }

    use_points = payment_type.endswith('_points')
    payment_key = payment_map.get(payment_type)

    if not payment_key:
        await query.answer("نوع الدفع غير صحيح!", show_alert=True)
        return PAYMENT

    amount = PRICES.get(payment_key, 0)

    # الباقي كما هو...

    
    if use_points:
        # الدفع بالنقاط
        payment_success = await process_payment(user_id, payment_key, pool, use_points=True)
        if payment_success:
            await query.answer(f"تم الدفع بنجاح باستخدام {amount} نقطة!", show_alert=True)
            await query.edit_message_text(
                text="❗️الخطوة التالية: أرسل يوزر القناة (مثال: @ChannelName) أو حول رسالة من القناة\n\n"
                     "⚠️ يجب أن يكون البوت أدمن في القناة",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data='back_to_main')]])
            )
            return WAITING_FOR_WINNERS
        else:
            await query.answer("رصيد النقاط غير كافي!", show_alert=True)
            return PAYMENT
    else:
        # إرسال فاتورة الدفع للنجوم
        description = "اشتراك شهري" if payment_key == 'premium_month' else "دفع لمرة واحدة"
        
        prices = [LabeledPrice(label=description, amount=amount)]
        
        try:
            await context.bot.send_invoice(
                chat_id=query.message.chat_id,
                title=description,
                description=f"{description} مقابل {amount} نجوم تليجرام",
                payload=f"{payment_key}_{user_id}_{amount}",
                provider_token="",  # يترك فارغًا
                currency=STARS_CURRENCY,
                prices=prices
            )
            return PAYMENT
        except Exception as e:
            logger.error(f"Error sending invoice: {e}")
            await query.answer("حدث خطأ أثناء إعداد عملية الدفع. يرجى المحاولة لاحقًا.", show_alert=True)
            return PAYMENT

# تعديل دالة handle_link_channel
async def handle_link_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ربط القناة الرئيسيّة أو حفظ قناة الشرط (بدون ربط فعلى)."""
    user_id   = update.message.from_user.id
    pool      = context.bot_data.get('pool')
    purpose   = context.user_data.get('link_channel_purpose')      # 'main_channel' أو 'condition_channel'

    try:
        # الحصول على كائن القناة سواء كانت رسالة محوَّلة أو يوزر مكتوب
        if update.message.forward_from_chat:
            chat = update.message.forward_from_chat
        else:
            txt  = update.message.text.strip().replace('https://t.me/', '').lstrip('@')
            chat = await context.bot.get_chat(f"@{txt}")

        # -----------------------------------------------------------
        # 1)  ربط القناة الرئيسيّة  ➜  يشترط أن يكون البوت مشرفًا
        # -----------------------------------------------------------
        if purpose == 'main_channel':
            admins = await chat.get_administrators()
            if not any(ad.user.id == context.bot.id for ad in admins):
                await update.message.reply_text("❌ البوت ليس مشرفًا فى هذه القناة!")
                return LINK_CHANNEL

            channel_info = f"{chat.id}|{chat.username}" if chat.username else str(chat.id)
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET linked_channel = $1 WHERE telegram_id = $2",
                    channel_info, user_id
                )

            await update.message.reply_text(
                f"✅ تم ربط القناة بنجاح!\n\nاسم القناة: {chat.title}\n"
                f"{'@'+chat.username if chat.username else 'ID: '+str(chat.id)}"
            )
            await show_main_menu(update, context)
            return MAIN_MENU

        # -----------------------------------------------------------
        # 2)  قناة الشرط  ➜  حفظ مؤقّت فقط (لا حاجة أن يكون البوت مشرفًا)
        # -----------------------------------------------------------
        else:  # purpose == 'condition_channel'
            condition = f"@{chat.username}" if chat.username else str(chat.id)
            context.user_data['required_channel'] = condition     # يُستخدم لاحقًا فى السحب

            await update.message.reply_text(
                "✅ تم حفظ قناة الشرط!\n\nاختر الآن عدد الفائزين:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(str(i), callback_data=f'winners_{i}') for i in [1, 2, 3]],
                    [InlineKeyboardButton(str(i), callback_data=f'winners_{i}') for i in [4, 5, 6]],
                    [InlineKeyboardButton(str(i), callback_data=f'winners_{i}') for i in [7, 8, 9]],
                    [InlineKeyboardButton("10", callback_data='winners_10')],
                    [InlineKeyboardButton("رجوع", callback_data='back_to_main')]
                ])
            )
            return WAITING_FOR_WINNERS

    except Exception as e:
        logger.error(f"Error in handle_link_channel: {e}")
        await update.message.reply_text(
            "❌ تعذّر التعرف على القناة. تأكَّد أن القناة عامة وأن المعرّف صحيح."
        )
        return LINK_CHANNEL

        
    except Exception as e:
        logger.error(f"Error linking channel: {e}")
        await update.message.reply_text("""
❌ حدث خطأ أثناء ربط القناة. تأكد من:
1. القناة عامة (ليست خاصة)
2. البوت مضاف كمسؤول بكل الصلاحيات
3. اليوزر صحيح (مثل @ChannelName أو https://t.me/ChannelName)
""")
        return LINK_CHANNEL

# تعديل دالة link_channel
async def link_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['link_channel_purpose'] = 'main_channel'
    
    await query.edit_message_text(
        text="أرسل معرف القناة الرئيسية أو رابطها لربطها بالبوت:\n\n"
             "يجب أن يكون البوت مشرفًا في القناة",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data='back_to_main')]])
    )
    
    return LINK_CHANNEL

# تعديل دالة add_channel
async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    pool = context.bot_data.get('pool')
    
    if not pool:
        await query.answer("حدث خطأ في النظام. يرجى المحاولة لاحقًا.", show_alert=True)
        return MAIN_MENU
    
    user_status = await check_user_payment_status(user_id, pool)
    
    if not user_status['is_premium'] and user_id not in ADMINS:
        await query.answer()
        context.user_data['link_channel_purpose'] = 'condition_channel'
        
        keyboard = [
            [InlineKeyboardButton(f"اشتراك شهري ({PRICES['premium_month']} نجمة)", callback_data='upgrade_month')],
            [InlineKeyboardButton(f"دفع لمرة واحدة ({PRICES['add_channel_once']} نجمة)", callback_data='upgrade_once')],
            [InlineKeyboardButton(f"دفع بالنقاط ({PRICES['add_channel_once']} نقطة)", callback_data='upgrade_once_points')],
            [InlineKeyboardButton("رجوع", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text=f"♻ ميزة إضافة قناة الشرط\n\n"
                 f"مع هذه الميزة، يمكنك تعيين قناة كشرط لدخول السحب.\n\n"
                 f"🔰 متاح فقط لمستخدمي النسخة المدفوعة\n"
                 f"💳 لديك {user_status['stars']} نجمة و {user_status['points']} نقطة\n"
                 f"اختر طريقة الدفع:",
            reply_markup=reply_markup
        )
        
        return PAYMENT
    else:
        await query.answer()
        context.user_data['link_channel_purpose'] = 'condition_channel'
        
        await query.edit_message_text(
            text="❗️الخطوة التالية: أرسل يوزر القناة الشرط (مثال: @ChannelName) أو حول رسالة من القناة\n\n"
                 "⚠️ يجب أن يكون البوت أدمن في القناة",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data='back_to_main')]])
        )
        
        return WAITING_FOR_WINNERS

# ... (بقية الدوال تبقى كما هي)

async def skip_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['required_channel'] = None
    await query.edit_message_text(
        text="الآن اختر عدد الفائزين:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(str(i), callback_data=f'winners_{i}') for i in [1, 2, 3]],
            [InlineKeyboardButton(str(i), callback_data=f'winners_{i}') for i in [4, 5, 6]],
            [InlineKeyboardButton(str(i), callback_data=f'winners_{i}') for i in [7, 8, 9]],
            [InlineKeyboardButton("10", callback_data='winners_10')],
            [InlineKeyboardButton("رجوع", callback_data='back_to_main')]
        ])
    )
    return WAITING_FOR_WINNERS

async def set_winners(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    winners_count = int(query.data.split('_')[1])
    pool = context.bot_data.get('pool')

    context.user_data['winners_count'] = winners_count

    try:
        async with pool.acquire() as conn:
            roulette_id = await conn.fetchval("""
                INSERT INTO roulettes (
                    creator_id, message, condition_channel_id, winner_count, is_active
                ) VALUES ($1, $2, $3, $4, TRUE)
                RETURNING id
            """, user_id, context.user_data['roulette_text'], 
                   context.user_data.get('required_channel'), winners_count)

            roulette_text = context.user_data['roulette_text']
            required_channel = context.user_data.get('required_channel')

            message_text = f"{roulette_text}\n\n"
            if required_channel:
                message_text += f"⚡ شرط السحب: الاشتراك في {required_channel}\n\n"
            message_text += f"عدد المشاركين: 0\n\nروليت باندا @Roulette_Panda_Bot"

            keyboard = [
                [InlineKeyboardButton("المشاركة في السحب", callback_data=f'join_{roulette_id}')],
                [
                    InlineKeyboardButton("ابدأ السحب", callback_data=f'draw_{roulette_id}'),
                    InlineKeyboardButton("أوقف المشاركة", callback_data=f'stop_{roulette_id}')
                ],
                [InlineKeyboardButton("🔔 ذكرني إذا فزت 💌", callback_data='remind_me')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # نحاول النشر في القناة فقط، وإن فشل نوقف العملية
            user_status = await check_user_payment_status(user_id, pool)
            channel_info = user_status.get('linked_channel')

            if not channel_info:
                await query.answer("❌ لا توجد قناة مربوطة!", show_alert=True)
                return MAIN_MENU

            channel_id = channel_info.split('|')[0]
            try:
                message = await context.bot.send_message(
                    chat_id=int(channel_id),
                    text=message_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"❌ فشل النشر في القناة: {e}")
                await query.answer("❌ فشل في نشر السحب بالقناة. تأكد أن البوت مشرف في القناة.", show_alert=True)
                return MAIN_MENU

            # تحديث بيانات السحب
            await conn.execute("""
                UPDATE roulettes 
                SET message_id = $1, chat_id = $2, channel_id = $3
                WHERE id = $4
            """, message.message_id, message.chat.id, channel_info, roulette_id)

            # إرسال لوحة التحكم الخاصة بصاحب السحب
            manage_keyboard = [
                [InlineKeyboardButton("🎲 ابدأ السحب", callback_data=f'draw_{roulette_id}')],
                [InlineKeyboardButton("⛔ أوقف المشاركة", callback_data=f'stop_{roulette_id}')],
                [InlineKeyboardButton("👥 عرض المشاركين", callback_data=f'view_participants_{roulette_id}')]
            ]

            await context.bot.send_message(
                chat_id=user_id,
                text="✅ تم إنشاء السحب بنجاح!\n\nيمكنك إدارة السحب من هنا:",
                reply_markup=InlineKeyboardMarkup(manage_keyboard)
            )

            return MAIN_MENU

    except Exception as e:
        logger.error(f"Error in set_winners: {e}")
        await query.answer("❌ حدث خطأ غير متوقع. حاول لاحقًا!", show_alert=True)
        return MAIN_MENU


async def join_roulette(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    roulette_id = int(query.data.split('_')[1])
    pool = context.bot_data.get('pool')
    
    async with pool.acquire() as conn:
        roulette = await conn.fetchrow("""
            SELECT * FROM roulettes 
            WHERE id = $1 AND is_active = TRUE
        """, roulette_id)
        
        if not roulette:
            await query.answer("هذا السحب لم يعد متاحًا!", show_alert=True)
            return
        
        # التحقق من الاشتراك في القناة المربوطة (التي ربطها منشئ السحب)
        try:
            # الحصول على معلومات القناة المربوطة
            creator_info = await conn.fetchrow("""
                SELECT linked_channel FROM users WHERE telegram_id = $1
            """, roulette['creator_id'])
            
            if creator_info and creator_info['linked_channel']:
                # استخراج معرف القناة من البيانات المخزنة (الصيغة: "channel_id|channel_username" أو "channel_id")
                channel_parts = creator_info['linked_channel'].split('|')
                channel_id = channel_parts[0]
                channel_username = channel_parts[1] if len(channel_parts) > 1 else None
                
                # التحقق من الاشتراك في القناة
                try:
                    member = await context.bot.get_chat_member(chat_id=int(channel_id), user_id=user.id)
                    if member.status not in ['member', 'administrator', 'creator']:
                        channel_ref = f"@{channel_username}" if channel_username else f"القناة (ID: {channel_id})"
                        await query.answer(f"يجب الاشتراك في {channel_ref} أولاً!", show_alert=True)
                        
                        # إرسال رسالة مع زر للاشتراك إذا كان هناك يوزر للقناة
                        if channel_username:
                            keyboard = [
                                [InlineKeyboardButton("اشترك في القناة", url=f"https://t.me/{channel_username}")],
                                [InlineKeyboardButton("لقد اشتركت ✅", callback_data=f'join_{roulette_id}')]
                            ]
                            reply_markup = InlineKeyboardMarkup(keyboard)
                            
                            await context.bot.send_message(
                                chat_id=user.id,
                                text=f"⚠️ يجب الاشتراك في القناة @{channel_username} أولاً للمشاركة في السحب",
                                reply_markup=reply_markup
                            )
                        return
                except Exception as e:
                    logger.error(f"Error checking linked channel membership: {e}")
                    await query.answer("حدث خطأ أثناء التحقق من اشتراكك. حاول مرة أخرى!", show_alert=True)
                    return
        except Exception as e:
            logger.error(f"Error getting creator's linked channel: {e}")
            await query.answer("حدث خطأ في النظام. يرجى المحاولة لاحقًا!", show_alert=True)
            return
        
        # التحقق من الاشتراك في قناة الشرط إذا وجدت
        if roulette['condition_channel_id']:
            try:
                condition_channel = roulette['condition_channel_id']
                if not condition_channel.startswith('@'):
                    condition_channel = f"@{condition_channel}"
                
                member = await context.bot.get_chat_member(condition_channel, user.id)
                if member.status not in ['member', 'administrator', 'creator']:
                    await query.answer(f"يجب الاشتراك في {condition_channel} أولاً!", show_alert=True)
                    return
            except Exception as e:
                logger.error(f"Error checking condition channel membership: {e}")
                await query.answer("حدث خطأ أثناء التحقق من اشتراكك. حاول مرة أخرى!", show_alert=True)
                return
        
        # باقي الكود كما هو...
        existing = await conn.fetchrow("""
            SELECT 1 FROM participants 
            WHERE roulette_id = $1 AND user_id = $2
        """, roulette_id, user.id)
        
        if existing:
            await query.answer("لقد شاركت بالفعل في هذا السحب!", show_alert=True)
            return
        
        await conn.execute("""
            INSERT INTO participants (roulette_id, user_id, username, full_name)
            VALUES ($1, $2, $3, $4)
        """, roulette_id, user.id, user.username, user.full_name)
        
        count = await conn.fetchval("""
            SELECT COUNT(*) FROM participants 
            WHERE roulette_id = $1
        """, roulette_id)
        
        try:
            original_text = query.message.text
            new_text = re.sub(
                r'عدد المشاركين: \d+', 
                f'عدد المشاركين: {count}', 
                original_text
            )
            
            await query.edit_message_text(
                text=new_text,
                reply_markup=query.message.reply_markup,
                parse_mode=ParseMode.HTML
            )
            
            await context.bot.send_message(
                chat_id=roulette['creator_id'],
                text=f"مشارك جديد في سحبك!\n\n👤 الاسم: {user.full_name}\n"
                     f"📌 اليوزر: @{user.username if user.username else 'غير متاح'}\n"
                     f"🆔 ID: {user.id}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "إزالة المشارك",
                    callback_data=f'remove_{roulette_id}_{user.id}'
                )]])
            )
            
            await query.answer("تمت مشاركتك في السحب بنجاح! 🎉", show_alert=True)
            
        except Exception as e:
            logger.error(f"Error updating roulette message: {e}")
            await query.answer("تمت مشاركتك، لكن حدث خطأ في تحديث الرسالة", show_alert=True)

async def draw_roulette(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    roulette_id = int(query.data.split('_')[1])
    pool = context.bot_data.get('pool')
    
    async with pool.acquire() as conn:
        roulette = await conn.fetchrow("""
            SELECT * FROM roulettes 
            WHERE id = $1 AND creator_id = $2
        """, roulette_id, user.id)
        
        if not roulette:
            await query.answer("هذا السحب لم يعد متاحًا أو ليس لديك صلاحية!", show_alert=True)
            return
        
        if roulette['is_active']:
            await query.answer("يجب إيقاف المشاركة أولاً قبل السحب!", show_alert=True)
            return
        
        participants = await conn.fetch("""
            SELECT user_id, username, full_name FROM participants 
            WHERE roulette_id = $1
        """, roulette_id)
        
        if len(participants) < roulette['winner_count']:
            await query.answer("عدد المشاركين أقل من عدد الفائزين المطلوب!", show_alert=True)
            return
        
        winners = random.sample(participants, roulette['winner_count'])
        
        message_text = f"{roulette['message']}\n\n🎉🎉🎉\n\n"
        if roulette['condition_channel_id']:
            message_text += f"الشرط: تشترك هنا {roulette['condition_channel_id']}\n\n"
        
        winners_text = "\n".join([f"🎖 {winner['full_name']} (@{winner['username']})" for winner in winners])
        message_text += f"الفائزون:\n{winners_text}\n\nروليت باندا @Roulette_Panda_Bot"
        
        await context.bot.edit_message_text(
            chat_id=roulette['chat_id'],
            message_id=roulette['message_id'],
            text=message_text,
            parse_mode=ParseMode.HTML
        )
        
        await query.answer("تم سحب الفائزين بنجاح!")
        
        for winner in winners:
            try:
                await context.bot.send_message(
                    chat_id=winner['user_id'],
                    text=f"🎉 مبروك! لقد فزت في السحب!\n\n{roulette['message']}"
                )
            except Exception as e:
                logger.error(f"Failed to notify winner {winner['user_id']}: {e}")
        
        await conn.execute("""
            UPDATE roulettes 
            SET is_active = FALSE 
            WHERE id = $1
        """, roulette_id)

async def stop_participation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    roulette_id = int(query.data.split('_')[1])
    pool = context.bot_data.get('pool')
    
    async with pool.acquire() as conn:
        # التحقق من حالة السحب الحالية
        roulette = await conn.fetchrow("""
            SELECT is_active, chat_id, message_id FROM roulettes 
            WHERE id = $1 AND creator_id = $2
        """, roulette_id, user.id)
        
        if not roulette:
            await query.answer("ليس لديك صلاحية لإدارة هذا السحب!", show_alert=True)
            return
            
        new_status = not roulette['is_active']
        
        # تحديث حالة السحب في قاعدة البيانات
        result = await conn.execute("""
            UPDATE roulettes 
            SET is_active = $1 
            WHERE id = $2 AND creator_id = $3
        """, new_status, roulette_id, user.id)
        
        if result.split()[1] == '0':
            await query.answer("ليس لديك صلاحية لإدارة هذا السحب!", show_alert=True)
            return
        
        # إنشاء لوحة المفاتيح الجديدة
        keyboard = [
            [InlineKeyboardButton("المشاركة في السحب", callback_data=f'join_{roulette_id}')],
            [
                InlineKeyboardButton("🎲 ابدأ السحب", callback_data=f'draw_{roulette_id}'),
                InlineKeyboardButton("⏸ استئناف المشاركة" if new_status else "⏹ أوقف المشاركة", 
                                   callback_data=f'stop_{roulette_id}')
            ],
            [InlineKeyboardButton("🔔 ذكرني إذا فزت 💌", callback_data='remind_me')]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            # تحديث الرسالة الأصلية في القناة
            await context.bot.edit_message_reply_markup(
                chat_id=roulette['chat_id'],
                message_id=roulette['message_id'],
                reply_markup=reply_markup
            )
            
            # إرسال رسالة تأكيد للمنشئ
            status_text = "تم استئناف المشاركة" if new_status else "تم إيقاف المشاركة"
            await query.answer(f"{status_text} بنجاح", show_alert=True)
            
            # تحديث رسالة التحكم الخاصة بالمنشئ
            manage_keyboard = [
                [InlineKeyboardButton("🎲 ابدأ السحب", callback_data=f'draw_{roulette_id}')],
                [InlineKeyboardButton("⏸ استئناف المشاركة" if new_status else "⏹ أوقف المشاركة", 
                                   callback_data=f'stop_{roulette_id}')],
                [InlineKeyboardButton("👥 عرض المشاركين", callback_data=f'view_participants_{roulette_id}')]
            ]
            
            await context.bot.send_message(
                chat_id=user.id,
                text=f"✅ {status_text} في السحب بنجاح!",
                reply_markup=InlineKeyboardMarkup(manage_keyboard)
            )
            
        except Exception as e:
            logger.error(f"Error updating message buttons: {e}")
            await query.answer("تم تغيير الحالة ولكن حدث خطأ في تحديث الرسالة", show_alert=True)

async def view_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    roulette_id = int(query.data.split('_')[2])
    pool = context.bot_data.get('pool')
    
    async with pool.acquire() as conn:
        participants = await conn.fetch("""
            SELECT full_name, username, user_id 
            FROM participants 
            WHERE roulette_id = $1
            ORDER BY joined_at
        """, roulette_id)
        
        if not participants:
            await query.answer("لا يوجد مشاركون بعد!", show_alert=True)
            return
        
        participants_text = "\n".join(
            [f"{i+1}. {p['full_name']} (@{p['username']}) - {p['user_id']}" 
             for i, p in enumerate(participants)]
        )
        
        await query.answer()
        await context.bot.send_message(
            chat_id=user.id,
            text=f"قائمة المشاركين في السحب:\n\n{participants_text}",
            parse_mode=ParseMode.HTML
        )

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    
    try:
        await show_main_menu(update, context)
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Error in back_to_main: {e}")
        await query.answer("حدث خطأ أثناء التحميل، يرجى المحاولة مرة أخرى", show_alert=True)
    
    return MAIN_MENU

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await show_admin_menu(update, context)
    return ADMIN_MENU

async def show_donate_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    pool = context.bot_data.get('pool')
    
    if not pool:
        await query.answer("حدث خطأ في النظام. يرجى المحاولة لاحقًا.", show_alert=True)
        return
    
    user_status = await check_user_payment_status(user_id, pool)
    
    keyboard = [
        [InlineKeyboardButton(f"تبرع بـ {PRICES['donate']} نجمة", callback_data='donate')],
        [InlineKeyboardButton("رجوع", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=f"♻ قائمة التبرع بالنجوم\n\n"
             f"يمكنك التبرع للمطور لدعم استمرار البوت\n\n"
             f"⭐ رصيدك الحالي: {user_status['stars']} نجمة\n"
             f"اختر المبلغ الذي تريد التبرع به:",
        reply_markup=reply_markup
    )

async def handle_donate_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    amount = PRICES['donate']
    
    prices = [LabeledPrice(label="تبرع للمطور", amount=amount)]
    
    try:
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title="التبرع للمطور",
            description=f"التبرع للمطور مقابل {amount} نجوم تليجرام",
            payload=f"donation_{query.from_user.id}_{amount}",
            provider_token="",  # يترك فارغًا كما أوصى صديقك
            currency=STARS_CURRENCY,
            prices=prices
        )
    except Exception as e:
        logger.error(f"Error sending invoice: {e}")
        await query.answer("حدث خطأ أثناء إعداد عملية الدفع. يرجى المحاولة لاحقًا.", show_alert=True)

async def handle_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    try:
        await context.bot.answer_pre_checkout_query(query.id, ok=True)
    except Exception as e:
        logger.error(f"Error in pre-checkout: {e}")

async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payment = update.message.successful_payment
    user = update.message.from_user
    amount = payment.total_amount
    pool = context.bot_data.get('pool')
    
    if pool:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO donations (donor_id, amount)
                VALUES ($1, $2)
            """, user.id, amount)
            
            await conn.execute("""
                UPDATE users 
                SET stars = stars + $1 
                WHERE telegram_id = $2
            """, amount, user.id)
    
    donation_details = (
        f"🎉 تم التبرع! \n\n"
        f"👤 الاسم: {user.full_name}\n"
        f"📌 اليوزر: @{user.username if user.username else 'غير متاح'}\n"
        f"🆔 الـ ID: {user.id}\n"
        f"💰 المبلغ: {amount} نجمة\n"
        f"⏰ الوقت: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    
    keyboard = [[InlineKeyboardButton("التحدث مع المتبرع", url=f"tg://user?id={user.id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(
                admin_id,
                donation_details,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")
    
    await update.message.reply_text(
        "✅ تم قبول الدفع بنجاح! شكراً لدعمك.\n"
        "سيتم استخدام هذه الأموال لتحسين البوت وتقديم المزيد من الميزات."
    )

async def handle_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    user_id = query.from_user.id
    payment_type = query.data
    pool = context.bot_data.get('pool')
    
    if not pool:
        await query.answer("حدث خطأ في النظام. يرجى المحاولة لاحقًا.", show_alert=True)
        return MAIN_MENU

    # تحويل أنواع الدفع إلى المفاتيح الموجودة في PRICES
    payment_map = {
        'upgrade_month': 'premium_month',
        'upgrade_once': 'add_channel_once',
        'upgrade_month_points': 'premium_month',
        'upgrade_once_points': 'add_channel_once'
    }

    use_points = payment_type.endswith('_points')
    payment_key = payment_map.get(payment_type)

    if not payment_key:
        await query.answer("نوع الدفع غير صحيح!", show_alert=True)
        return PAYMENT

    amount = PRICES.get(payment_key, 0)

    if use_points:
        # الدفع باستخدام النقاط
        payment_success = await process_payment(user_id, payment_key, pool, use_points=True)
        if payment_success:
            await query.answer(f"تم الدفع بنجاح باستخدام {amount} نقطة!", show_alert=True)
            await query.edit_message_text(
                text="❗️الخطوة التالية: أرسل يوزر القناة (مثال: @ChannelName) أو حول رسالة من القناة\n\n"
                     "⚠️ يجب أن يكون البوت أدمن في القناة",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data='back_to_main')]])
            )
            return WAITING_FOR_WINNERS
        else:
            await query.answer("رصيد النقاط غير كافي!", show_alert=True)
            return PAYMENT
    else:
        # إرسال فاتورة الدفع باستخدام النجوم
        description = "اشتراك شهري" if payment_key == 'premium_month' else "دفع لمرة واحدة"
        prices = [LabeledPrice(label=description, amount=amount)]
        
        try:
            await context.bot.send_invoice(
                chat_id=query.message.chat_id,
                title=description,
                description=f"{description} مقابل {amount} نجوم تليجرام",
                payload=f"{payment_key}_{user_id}_{amount}",
                provider_token="",  # أدخل Provider Token هنا إن وجد
                currency=STARS_CURRENCY,
                prices=prices
            )
            return PAYMENT
        except Exception as e:
            logger.error(f"Error sending invoice: {e}")
            await query.answer("حدث خطأ أثناء إعداد عملية الدفع. يرجى المحاولة لاحقًا.", show_alert=True)
            return PAYMENT


async def handle_link_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    current_state = context.user_data.get('link_channel_purpose')
    
    try:
        if update.message.forward_from_chat:
            chat = update.message.forward_from_chat
        else:
            text = update.message.text.strip()
            text = text.replace('https://t.me/', '').replace('@', '')
            chat = await context.bot.get_chat(f"@{text}" if not text.startswith('@') else text)
        
        admins = await chat.get_administrators()
        bot_id = context.bot.id
        if not any(admin.user.id == bot_id for admin in admins):
            await update.message.reply_text("❌ البوت ليس مشرفًا! يرجى ترقيته أولاً.")
            return LINK_CHANNEL if current_state == 'main_channel' else WAITING_FOR_WINNERS

        if current_state == 'main_channel':
            # ربط القناة الرئيسية فقط (مع رسالة تأكيد)
            channel_info = f"{chat.id}|{chat.username}" if chat.username else str(chat.id)
            
            async with context.bot_data['pool'].acquire() as conn:
                await conn.execute("UPDATE users SET linked_channel = $1 WHERE telegram_id = $2", channel_info, user_id)
            
            await update.message.reply_text(
                f"✅ تم ربط القناة الرئيسية بنجاح!\n\n"
                f"اسم القناة: {chat.title}\n"
                f"{'@' + chat.username if chat.username else 'ID: ' + str(chat.id)}"
            )
            await show_main_menu(update, context)
            return MAIN_MENU
        else:
            # قناة الشرط (بدون أي رسائل تأكيد)
            context.user_data['required_channel'] = f"@{chat.username}" if chat.username else str(chat.id)
            
            # الانتقال المباشر لاختيار الفائزين
            await update.message.reply_text(
                "الآن اختر عدد الفائزين:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(str(i), callback_data=f'winners_{i}') for i in [1, 2, 3]],
                    [InlineKeyboardButton(str(i), callback_data=f'winners_{i}') for i in [4, 5, 6]],
                    [InlineKeyboardButton(str(i), callback_data=f'winners_{i}') for i in [7, 8, 9]],
                    [InlineKeyboardButton("10", callback_data='winners_10')],
                    [InlineKeyboardButton("رجوع", callback_data='back_to_main')]
                ])
            )
            return WAITING_FOR_WINNERS
        
    except Exception as e:
        logger.error(f"Error linking channel: {e}")
        await update.message.reply_text("""
❌ حدث خطأ! تأكد من:
1. القناة عامة
2. البوت مشرف
3. اليوزر صحيح (مثل @ChannelName)
""")
        return LINK_CHANNEL if current_state == 'main_channel' else WAITING_FOR_WINNERS

async def unlink_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    pool = context.bot_data.get('pool')
    
    async with pool.acquire() as conn:
        user_status = await check_user_payment_status(user_id, pool)
        if not user_status['linked_channel']:
            await query.answer("لا يوجد قناة مربوطة!", show_alert=True)
            return MAIN_MENU
        
        await conn.execute("""
            UPDATE users 
            SET linked_channel = NULL 
            WHERE telegram_id = $1
        """, user_id)
    
    await query.answer("تم فصل القناة بنجاح", show_alert=True)
    await show_main_menu(update, context)
    return MAIN_MENU

async def remind_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("سيتم إعلامك إذا فزت بأي سحب مستقبلي", show_alert=True)

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    keyboard = [[InlineKeyboardButton("تواصل مع الدعم", url=f"https://t.me/{SUPPORT_USERNAME[1:]}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=f"للاستفسارات أو المشاكل الفنية، يرجى التواصل مع الدعم:\n\n{SUPPORT_USERNAME}",
        reply_markup=reply_markup
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    pool = context.bot_data.get('pool')
    
    user_status = await check_user_payment_status(user_id, pool)
    
    keyboard = [
        [InlineKeyboardButton("شراء نقاط بخصم 30%", url=f"https://t.me/{SUPPORT_USERNAME[1:]}")],
        [InlineKeyboardButton("رجوع", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=f"رصيدك الحالي:\n\n"
            #  f"⭐ النجوم: {user_status['stars']}\n"
             f"📌 النقاط: {user_status['points']}\n\n"
             f"يمكنك شراء نقاط بخصم 30% (أرخص من النجوم)",
        reply_markup=reply_markup
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="حدث خطأ في البوت:", exc_info=context.error)
    
    if update.callback_query:
        await update.callback_query.answer("حدث خطأ غير متوقع. يرجى المحاولة مرة أخرى!", show_alert=True)
    elif update.message:
        await update.message.reply_text("حدث خطأ غير متوقع. يرجى المحاولة مرة أخرى!")

async def main() -> None:
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    pool = await init_db()
    application = Application.builder().token(TOKEN).build()
    application.bot_data['pool'] = pool

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            START: [
                CallbackQueryHandler(subscribed, pattern='^subscribed$')
            ],
            MAIN_MENU: [
                CallbackQueryHandler(create_roulette, pattern='^create_roulette$'),
                CallbackQueryHandler(link_channel, pattern='^link_channel$'),
                CallbackQueryHandler(unlink_channel, pattern='^unlink_channel$'),
                CallbackQueryHandler(show_donate_menu, pattern='^donate_menu$'),
                CallbackQueryHandler(remind_me, pattern='^remind_me$'),
                CallbackQueryHandler(support, pattern='^support$'),
                CallbackQueryHandler(balance, pattern='^balance$'),
                CallbackQueryHandler(back_to_main, pattern='^back_to_main$'),
            ],
            ADMIN_MENU: [
                CallbackQueryHandler(admin_add_points, pattern='^add_points$'),
                CallbackQueryHandler(admin_menu, pattern='^admin_menu$'),
                CallbackQueryHandler(back_to_main, pattern='^back_to_main$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_handle_points)
            ],
            WAITING_FOR_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_roulette_text),
                CallbackQueryHandler(back_to_main, pattern='^back_to_main$')
            ],
            ADD_CHANNEL: [
                CallbackQueryHandler(add_channel, pattern='^add_channel$'),
                CallbackQueryHandler(skip_channel, pattern='^skip_channel$'),
                CallbackQueryHandler(back_to_main, pattern='^back_to_main$')
            ],
            PAYMENT: [
                CallbackQueryHandler(handle_payment, pattern='^(upgrade_month|upgrade_once|upgrade_month_points|upgrade_once_points)$'),
                CallbackQueryHandler(back_to_main, pattern='^back_to_main$'),
                MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment)
            ],
            WAITING_FOR_WINNERS: [
                CallbackQueryHandler(set_winners, pattern=r'^winners_\d+$'),
                CallbackQueryHandler(back_to_main, pattern='^back_to_main$'),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link_channel)
            ],
            LINK_CHANNEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link_channel),
                CallbackQueryHandler(back_to_main, pattern='^back_to_main$')
            ]
        },
        fallbacks=[CommandHandler('start', start)],
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(join_roulette, pattern='^join_'))
    application.add_handler(CallbackQueryHandler(draw_roulette, pattern='^draw_'))
    application.add_handler(CallbackQueryHandler(stop_participation, pattern='^stop_'))
    application.add_handler(CallbackQueryHandler(view_participants, pattern='^view_participants_'))
    application.add_handler(CallbackQueryHandler(back_to_main, pattern='^back_to_main$'))
    application.add_handler(CallbackQueryHandler(handle_donate_selection, pattern='^donate$'))
    application.add_handler(CallbackQueryHandler(admin_menu, pattern='^admin_menu$'))
    application.add_handler(PreCheckoutQueryHandler(handle_pre_checkout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment))
    application.add_error_handler(error_handler)

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Stopping bot...")
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await pool.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
