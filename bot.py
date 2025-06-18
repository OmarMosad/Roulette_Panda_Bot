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
from typing import Dict, List, Optional
import asyncio

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
ADMINS = [int(id) for id in os.getenv('ADMIN_IDS').split(',')]
DATABASE_URL = os.getenv('DATABASE_URL')

# حالات المحادثة
START, MAIN_MENU, CREATE_ROULETTE, ADD_CHANNEL, PAYMENT, WAITING_FOR_TEXT, WAITING_FOR_WINNERS = range(7)

# أسعار الخدمات
PRICES = {
    'premium_month': 100,  # 100 نجمة للاشتراك الشهري
    'add_channel_once': 5,  # 5 نجوم لإضافة قناة لمرة واحدة
    'donate': 15  # 15 نجمة للتبرع الأساسي
}

# عملة النجوم
STARS_CURRENCY = "XTR"

# تهيئة قاعدة البيانات
async def init_db():
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute("""
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
        
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            stars INTEGER DEFAULT 0,
            is_premium BOOLEAN DEFAULT FALSE,
            premium_expiry TIMESTAMP,
            created_at TIMESTAMP DEFAULT now(),
            updated_at TIMESTAMP DEFAULT now()
        );
        
        CREATE TABLE IF NOT EXISTS donations (
            id SERIAL PRIMARY KEY,
            donor_id BIGINT,
            amount INTEGER,
            donation_date TIMESTAMP DEFAULT now()
        );
        """)
    return pool

# التحقق من حالة المستخدم المدفوعة
async def check_user_payment_status(user_id: int, pool) -> Dict:
    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT is_premium, premium_expiry, stars 
            FROM users 
            WHERE telegram_id = $1
        """, user_id)
        
        if not user:
            await conn.execute("""
                INSERT INTO users (telegram_id) 
                VALUES ($1)
            """, user_id)
            return {'is_premium': False, 'premium_expiry': None, 'stars': 0}
        
        return {
            'is_premium': user['is_premium'],
            'premium_expiry': user['premium_expiry'],
            'stars': user['stars']
        }

# معالجة الدفع
async def process_payment(user_id: int, payment_type: str, pool) -> bool:
    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT stars FROM users WHERE telegram_id = $1
        """, user_id)
        
        if not user:
            return False
            
        required_stars = PRICES.get(payment_type, 0)
        if user['stars'] < required_stars:
            return False
            
        await conn.execute("""
            UPDATE users 
            SET stars = stars - $1 
            WHERE telegram_id = $2
        """, required_stars, user_id)
        
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
        """, user_id, payment_type, required_stars)
        
        return True

# ======== الوظائف الأساسية للبوت ========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    user_id = user.id
    
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
    
    await query.answer()
    await show_main_menu(update, context)
    return MAIN_MENU

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("إنشاء روليت", callback_data='create_roulette')],
        [InlineKeyboardButton("ربط القناة", callback_data='link_channel')],
        [InlineKeyboardButton("فصل القناة", callback_data='unlink_channel')],
        [InlineKeyboardButton(f"تبرع ({PRICES['donate']} نجمة)", callback_data='donate_menu')],
        [InlineKeyboardButton("ذكرني إذا فزت 🔔", callback_data='remind_me')],
        [InlineKeyboardButton("الدعم الفني", callback_data='support')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text="مرحبًا بك في القائمة الرئيسية لباندا روليت:",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            text="مرحبًا بك في القائمة الرئيسية لباندا روليت:",
            reply_markup=reply_markup
        )

async def create_roulette(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
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
        
        keyboard = [
            [InlineKeyboardButton(f"اشتراك شهري ({PRICES['premium_month']} نجمة)", callback_data='upgrade_month')],
            [InlineKeyboardButton(f"دفع لمرة واحدة ({PRICES['add_channel_once']} نجمة)", callback_data='upgrade_once')],
            [InlineKeyboardButton("رجوع", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text=f"♻ ميزة إضافة قناة الشرط\n\n"
                 f"مع هذه الميزة، يمكنك تعيين قناة كشرط لدخول السحب.\n\n"
                 f"🔰 متاح فقط لمستخدمي النسخة المدفوعة\n"
                 f"💳 لديك {user_status['stars']} نجمة\n"
                 f"اختر طريقة الدفع:",
            reply_markup=reply_markup
        )
        
        return PAYMENT
    else:
        await query.answer()
        await query.edit_message_text(
            text="أرسل يوزر القناة (مثال: @ChannelName) أو حول رسالة من القناة\n\n"
                 "يجب أن يكون البوت أدمن في القناة",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("رجوع", callback_data='back_to_main')]])
        )
        
        return WAITING_FOR_WINNERS

async def skip_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    context.user_data['required_channel'] = None
    
    await query.edit_message_text(
        text="اختر عدد الفائزين:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(str(i), callback_data=f'winners_{i}') for i in [1, 2, 3]],
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
    
    # حفظ الروليت في قاعدة البيانات
    async with pool.acquire() as conn:
        roulette_id = await conn.fetchval("""
            INSERT INTO roulettes (
                creator_id, message, condition_channel_id, winner_count, is_active
            ) VALUES ($1, $2, $3, $4, TRUE)
            RETURNING id
        """, user_id, context.user_data['roulette_text'], 
           context.user_data.get('required_channel'), winners_count)
    
    # إنشاء رسالة الروليت
    roulette_text = context.user_data['roulette_text']
    required_channel = context.user_data.get('required_channel')
    
    message_text = f"{roulette_text}\n\n❤❤\n\n"
    if required_channel:
        message_text += f"الشرط: تشترك هنا {required_channel}\n\n"
    message_text += f"عدد المشاركين: 0\n\nروليت باندا @Roulette_Panda_Bot"
    
    keyboard = [
        [InlineKeyboardButton("المشاركة في السحب", callback_data=f'join_{roulette_id}')]
    ]
    
    if 'chat_id' in context.user_data:
        message = await context.bot.send_message(
            chat_id=context.user_data['chat_id'],
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    else:
        message = await query.edit_message_text(
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    
    # تحديث الروليت في قاعدة البيانات بمعلومات الرسالة
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE roulettes 
            SET message_id = $1, chat_id = $2 
            WHERE id = $3
        """, message.message_id, message.chat.id, roulette_id)
    
    # إرسال رسالة للمنشئ لإدارة السحب
    manage_keyboard = [
        [InlineKeyboardButton("ابدأ السحب", callback_data=f'draw_{roulette_id}')],
        [InlineKeyboardButton("أوقف المشاركة", callback_data=f'stop_{roulette_id}')]
    ]
    
    await context.bot.send_message(
        chat_id=user_id,
        text="تم إنشاء الروليت بنجاح!\n\nيمكنك إدارته من هنا:",
        reply_markup=InlineKeyboardMarkup(manage_keyboard)
    )
    
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
        
        # التحقق من القناة المطلوبة
        if roulette['condition_channel_id']:
            try:
                member = await context.bot.get_chat_member(roulette['condition_channel_id'], user.id)
                if member.status not in ['member', 'administrator', 'creator']:
                    await query.answer(f"يجب الاشتراك في {roulette['condition_channel_id']} أولاً!", show_alert=True)
                    return
            except Exception as e:
                logger.error(f"Error checking channel membership: {e}")
                await query.answer("حدث خطأ أثناء التحقق من اشتراكك. حاول مرة أخرى!", show_alert=True)
                return
        
        # التحقق من المشاركة السابقة
        existing_participant = await conn.fetchrow("""
            SELECT 1 FROM participants 
            WHERE roulette_id = $1 AND user_id = $2
        """, roulette_id, user.id)
        
        if existing_participant:
            await query.answer("أنت بالفعل مشترك في هذا السحب!", show_alert=True)
            return
        
        # إضافة المشارك
        await conn.execute("""
            INSERT INTO participants (roulette_id, user_id, username, full_name)
            VALUES ($1, $2, $3, $4)
        """, roulette_id, user.id, user.username, user.full_name)
        
        # حساب عدد المشاركين
        participants_count = await conn.fetchval("""
            SELECT COUNT(*) FROM participants 
            WHERE roulette_id = $1
        """, roulette_id)
        
    # تحديث عدد المشاركين في الرسالة الأصلية
    message_text = query.message.text
    new_text = re.sub(r'عدد المشاركين: \d+', f'عدد المشاركين: {participants_count}', message_text)
    
    await query.edit_message_text(
        text=new_text,
        reply_markup=query.message.reply_markup,
        parse_mode=ParseMode.HTML
    )
    
    # إرسال إشعار للمنشئ
    await context.bot.send_message(
        chat_id=roulette['creator_id'],
        text=f"تم انضمام\nالاسم: {user.full_name}\nاليوزر: @{user.username if user.username else 'غير متاح'}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("استبعاد", callback_data=f'remove_{roulette_id}_{user.id}')]
        ])
    )
    
    await query.answer("تم انضمامك إلى السحب بنجاح!")

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
            SELECT user_id, username FROM participants 
            WHERE roulette_id = $1
        """, roulette_id)
        
        if len(participants) < roulette['winner_count']:
            await query.answer("عدد المشاركين أقل من عدد الفائزين المطلوب!", show_alert=True)
            return
        
        winners = random.sample(participants, roulette['winner_count'])
        
        # تحديث رسالة الروليت
        message_text = f"{roulette['message']}\n\n❤❤\n\n"
        if roulette['condition_channel_id']:
            message_text += f"الشرط: تشترك هنا {roulette['condition_channel_id']}\n\n"
        
        winners_text = ", ".join([f"@{winner['username']}" for winner in winners])
        message_text += f"لقد تم الانتهاء من السحب وتم الاعلان عن الفائزين:\n\n{winners_text}\n\nروليت باندا @Roulette_Panda_Bot"
        
        await context.bot.edit_message_text(
            chat_id=roulette['chat_id'],
            message_id=roulette['message_id'],
            text=message_text,
            parse_mode=ParseMode.HTML
        )
        
        await query.answer("تم سحب الفائزين بنجاح!")
        
        # إرسال إشعار للفائزين
        for winner in winners:
            try:
                await context.bot.send_message(
                    chat_id=winner['user_id'],
                    text=f"🎉 مبروك! لقد فزت في السحب!\n\n{roulette['message']}"
                )
            except Exception as e:
                logger.error(f"Failed to notify winner {winner['user_id']}: {e}")
        
        # تعطيل الروليت
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
        result = await conn.execute("""
            UPDATE roulettes 
            SET is_active = FALSE 
            WHERE id = $1 AND creator_id = $2
        """, roulette_id, user.id)
        
        if result.split()[1] == '0':
            await query.answer("ليس لديك صلاحية لإيقاف هذا السحب!", show_alert=True)
        else:
            await query.answer("تم إيقاف المشاركة في السحب!")

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await show_main_menu(update, context)
    return MAIN_MENU

# ======== نظام الدفع والنجوم ========

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
    
    if payment_type == 'upgrade_month':
        payment_success = await process_payment(user_id, 'premium_month', pool)
    elif payment_type == 'upgrade_once':
        payment_success = await process_payment(user_id, 'add_channel_once', pool)
    else:
        payment_success = False
    
    if payment_success:
        await query.answer("تمت عملية الدفع بنجاح!", show_alert=True)
        return await add_channel(update, context)
    else:
        await query.answer("رصيدك من النجوم غير كافي!", show_alert=True)
        return PAYMENT

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="حدث خطأ في البوت:", exc_info=context.error)
    
    if update.callback_query:
        await update.callback_query.answer("حدث خطأ غير متوقع. يرجى المحاولة مرة أخرى!", show_alert=True)
    elif update.message:
        await update.message.reply_text("حدث خطأ غير متوقع. يرجى المحاولة مرة أخرى!")

async def main() -> None:
    pool = await init_db()
    application = Application.builder().token(TOKEN).build()
    application.bot_data['pool'] = pool
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            START: [CallbackQueryHandler(subscribed, pattern='^subscribed$')],
            MAIN_MENU: [
                CallbackQueryHandler(create_roulette, pattern='^create_roulette$'),
                CallbackQueryHandler(back_to_main, pattern='^back_to_main$'),
                CallbackQueryHandler(show_donate_menu, pattern='^donate_menu$')
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
                CallbackQueryHandler(handle_payment, pattern='^(upgrade_month|upgrade_once)$'),
                CallbackQueryHandler(back_to_main, pattern='^back_to_main$')
            ],
            WAITING_FOR_WINNERS: [
                CallbackQueryHandler(set_winners, pattern=r'^winners_\d+$'),
                CallbackQueryHandler(back_to_main, pattern='^back_to_main$')
            ]
        },
        fallbacks=[CommandHandler('start', start)]
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(join_roulette, pattern='^join_'))
    application.add_handler(CallbackQueryHandler(draw_roulette, pattern='^draw_'))
    application.add_handler(CallbackQueryHandler(stop_participation, pattern='^stop_'))
    application.add_handler(CallbackQueryHandler(back_to_main, pattern='^back_to_main$'))
    application.add_handler(CallbackQueryHandler(handle_donate_selection, pattern='^donate$'))
    application.add_handler(PreCheckoutQueryHandler(handle_pre_checkout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment))
    application.add_error_handler(error_handler)
    
    await application.run_polling()

if __name__ == '__main__':
    asyncio.run(main())