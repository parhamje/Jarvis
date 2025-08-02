import logging
import sqlite3
import asyncio
import os
import random
import sys
import json
import aiohttp
from datetime import datetime, timedelta
from typing import Optional, List
import tempfile
import platform
import re

# Fix for Windows event loop policy
if platform.system() == 'Windows':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Import telegram libraries
try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
except ImportError as e:
    print(f"❌ خطا در وارد کردن telegram: {e}")
    sys.exit(1)

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.cron import CronTrigger
except ImportError as e:
    print(f"❌ خطا در وارد کردن apscheduler: {e}")
    sys.exit(1)

try:
    from gtts import gTTS
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False
    print("⚠️ gTTS در دسترس نیست. قابلیت صوتی غیرفعال است.")

# 🔧 Configuration from Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Validation
if not BOT_TOKEN:
    print("❌ BOT_TOKEN environment variable is required!")
    sys.exit(1)

if OWNER_ID == 0:
    print("❌ OWNER_ID environment variable is required!")
    sys.exit(1)

if not OPENROUTER_API_KEY:
    print("⚠️ OPENROUTER_API_KEY not set. AI features will be disabled.")

DATABASE_PATH = "jarvis_bot.db"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-3.5-turbo"

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class JarvisBot:
    def __init__(self):
        self.token = BOT_TOKEN
        self.owner_id = OWNER_ID
        self.application = None
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.scheduler = AsyncIOScheduler(event_loop=self.loop)
        self.db_path = DATABASE_PATH
        self.offline_mode = False
        self.offline_message = "🤖 جاروِیس در حال حاضر آفلاین است. پیام شما ثبت شد و بعداً پاسخ داده خواهد شد."
        
        # AI Configuration
        self.openrouter_api_key = OPENROUTER_API_KEY
        self.ai_enabled = OPENROUTER_API_KEY != "YOUR_OPENROUTER_API_KEY"
        self.default_model = DEFAULT_MODEL
        self.conversation_memory = {}
        
        # Initialize database
        self.init_database()
        
    def init_database(self):
        """Initialize SQLite database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Create tables
        tables = [
            '''CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed BOOLEAN DEFAULT FALSE
            )''',
            '''CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''',
            '''CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                reminder_time TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed BOOLEAN DEFAULT FALSE
            )''',
            '''CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                daily_tip_enabled BOOLEAN DEFAULT TRUE,
                daily_tip_time TEXT DEFAULT '09:00',
                voice_replies BOOLEAN DEFAULT FALSE,
                ai_enabled BOOLEAN DEFAULT TRUE,
                ai_model TEXT DEFAULT 'openai/gpt-3.5-turbo'
            )''',
            '''CREATE TABLE IF NOT EXISTS offline_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                message TEXT NOT NULL,
                received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''',
            '''CREATE TABLE IF NOT EXISTS ai_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                response TEXT NOT NULL,
                model_used TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )'''
        ]
        
        for table in tables:
            cursor.execute(table)
        
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")

    def is_owner(self, user_id: int) -> bool:
        return user_id == self.owner_id

    async def execute_function(self, function_name: str, parameters: dict, user_id: int) -> str:
        """Execute bot functions based on AI interpretation"""
        try:
            if function_name == "add_task":
                description = parameters.get("description", "")
                if not description:
                    return "❌ نیاز به توضیحات وظیفه دارم."
                
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("INSERT INTO tasks (user_id, description) VALUES (?, ?)", (user_id, description))
                conn.commit()
                conn.close()
                return f"✅ وظیفه اضافه شد: {description}"
            
            elif function_name == "list_tasks":
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT id, description, created_at FROM tasks WHERE user_id = ? AND completed = FALSE ORDER BY created_at", (user_id,))
                tasks = cursor.fetchall()
                conn.close()
                
                if not tasks:
                    return "📋 هیچ وظیفه‌ای در لیست نیست!"
                
                result = "📋 **وظایف باقی‌مانده:**\n\n"
                for i, (task_id, description, created_at) in enumerate(tasks, 1):
                    date = datetime.fromisoformat(created_at).strftime('%m/%d')
                    result += f"{i}. {description} 📅 {date}\n"
                return result
            
            elif function_name == "complete_task":
                task_number = parameters.get("task_number")
                if not task_number:
                    return "❌ کدام وظیفه را تکمیل کنم؟ شماره آن را بگویید."
                
                try:
                    task_number = int(task_number)
                except:
                    return "❌ شماره وظیفه باید عدد باشد."
                
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT id, description FROM tasks WHERE user_id = ? AND completed = FALSE ORDER BY created_at", (user_id,))
                tasks = cursor.fetchall()
                
                if not tasks or task_number < 1 or task_number > len(tasks):
                    conn.close()
                    return "❌ شماره وظیفه نامعتبر است."
                
                task_id, description = tasks[task_number - 1]
                cursor.execute("UPDATE tasks SET completed = TRUE WHERE id = ?", (task_id,))
                conn.commit()
                conn.close()
                return f"🎉 وظیفه تکمیل شد: {description}"
            
            elif function_name == "add_note":
                content = parameters.get("content", "")
                if not content:
                    return "❌ متن یادداشت را وارد کنید."
                
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("INSERT INTO notes (user_id, content) VALUES (?, ?)", (user_id, content))
                conn.commit()
                conn.close()
                return f"📝 یادداشت ذخیره شد: {content}"
            
            elif function_name == "list_notes":
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT content, created_at FROM notes WHERE user_id = ? ORDER BY created_at DESC LIMIT 10", (user_id,))
                notes = cursor.fetchall()
                conn.close()
                
                if not notes:
                    return "📝 هیچ یادداشتی موجود نیست!"
                
                result = "📝 **یادداشت‌ها:**\n\n"
                for content, created_at in notes:
                    date = datetime.fromisoformat(created_at).strftime('%m/%d')
                    result += f"💡 {content} 📅 {date}\n\n"
                return result
            
            elif function_name == "set_reminder":
                time_str = parameters.get("time", "")
                description = parameters.get("description", "")
                
                if not time_str or not description:
                    return "❌ زمان و توضیحات یادآوری لازم است. مثل: '30 دقیقه دیگر قرار ملاقات'"
                
                try:
                    reminder_time = self.parse_time_string(time_str)
                except ValueError as e:
                    return f"❌ {str(e)}"
                
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute("INSERT INTO reminders (user_id, description, reminder_time) VALUES (?, ?, ?)", 
                              (user_id, description, reminder_time.isoformat()))
                reminder_id = cursor.lastrowid
                conn.commit()
                conn.close()
                
                # Schedule reminder
                self.scheduler.add_job(
                    self.send_reminder,
                    DateTrigger(run_date=reminder_time),
                    args=[user_id, description, reminder_id],
                    id=f"reminder_{reminder_id}"
                )
                
                time_display = reminder_time.strftime('%m/%d %H:%M')
                return f"⏰ یادآوری تنظیم شد: {description} در {time_display}"
            
            elif function_name == "get_tip":
                tips = [
                    "💡 **برنامه‌نویسی:** همیشه کدتان را مستند کنید!",
                    "🌐 **شبکه:** TCP قابل اطمینان، UDP سریع‌تر است.",
                    "🔒 **امنیت:** رمزها را هاردکد نکنید، از متغیرهای محیطی استفاده کنید.",
                    "⚡ **کارایی:** الگوریتم O(n) بهتر از O(n²) است.",
                    "🐍 **Python:** از List Comprehension استفاده کنید: [x*2 for x in range(10)]",
                    "🗄️ **دیتابیس:** ایندکس روی ستون‌های پرجستجو سرعت را افزایش می‌دهد.",
                    "🔧 **Git:** از git stash برای ذخیره موقت استفاده کنید.",
                    "🎯 **تست:** کد بدون تست مثل ماشین بدون ترمز است!",
                ]
                return random.choice(tips)
            
            elif function_name == "get_quote":
                quotes = [
                    "💫 \"تنها راه انجام کار عالی این است که آنچه انجام می‌دهید را دوست داشته باشید.\" - استیو جابز",
                    "🌟 \"موفقیت نهایی نیست، شکست کشنده نیست: شجاعت ادامه دادن اهمیت دارد.\" - چرچیل",
                    "🚀 \"آینده متعلق به کسانی است که به رویاهایشان ایمان دارند.\" - الینور روزولت",
                    "💎 \"موفقیت مجموع تلاش‌های کوچک روزانه است.\" - رابرت کلیر",
                    "🌈 \"هر روز فرصت جدیدی است. امروز را بهترین روز زندگی‌تان کنید.\"",
                ]
                return random.choice(quotes)
            
            elif function_name == "get_summary":
                today = datetime.now().date()
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                
                cursor.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND completed = TRUE AND DATE(created_at) = ?", (user_id, today.isoformat()))
                completed_tasks = cursor.fetchone()[0]
                
                cursor.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ? AND completed = FALSE", (user_id,))
                pending_tasks = cursor.fetchone()[0]
                
                cursor.execute("SELECT COUNT(*) FROM notes WHERE user_id = ? AND DATE(created_at) = ?", (user_id, today.isoformat()))
                notes_today = cursor.fetchone()[0]
                
                cursor.execute("SELECT COUNT(*) FROM ai_conversations WHERE user_id = ? AND DATE(created_at) = ?", (user_id, today.isoformat()))
                ai_conversations = cursor.fetchone()[0]
                
                conn.close()
                
                return f"""📊 **خلاصه روزانه - {today.strftime('%Y/%m/%d')}**

✅ وظایف تکمیل شده: {completed_tasks}
📋 وظایف باقی‌مانده: {pending_tasks}
📝 یادداشت‌های امروز: {notes_today}
🤖 گفتگوهای AI: {ai_conversations}

💪 ادامه بدهید! هر قدم مهم است."""
            
            else:
                return f"❌ عملکرد '{function_name}' شناخته شده نیست."
                
        except Exception as e:
            logger.error(f"Error executing function {function_name}: {e}")
            return f"❌ خطا در اجرای عملکرد: {str(e)}"

    async def get_ai_response(self, user_id: int, message: str, context: str = None) -> str:
        """Get AI response with function calling capability"""
        if not self.ai_enabled:
            return "🤖 AI غیرفعال است. لطفاً API key را تنظیم کنید."

        try:
            # Get user's conversation history
            if user_id not in self.conversation_memory:
                self.conversation_memory[user_id] = []

            # Build enhanced system prompt with function calling
            system_prompt = f"""You are Jarvis, a helpful Persian/Farsi speaking AI assistant integrated into a Telegram bot. You can understand natural language and execute functions automatically.

IMPORTANT: You can execute the following functions by analyzing user intent:

1. add_task - When user wants to add a task/todo
   Parameters: {{"description": "task description"}}
   
2. list_tasks - When user asks to see tasks/todos
   Parameters: {{}}
   
3. complete_task - When user wants to mark a task as done
   Parameters: {{"task_number": "number"}}
   
4. add_note - When user wants to save a note
   Parameters: {{"content": "note content"}}
   
5. list_notes - When user asks to see notes
   Parameters: {{}}
   
6. set_reminder - When user wants to set a reminder
   Parameters: {{"time": "time format like 30m, 2h, 1d", "description": "reminder text"}}
   
7. get_tip - When user asks for tips or learning
   Parameters: {{}}
   
8. get_quote - When user asks for motivation or quotes
   Parameters: {{}}
   
9. get_summary - When user asks for daily summary
   Parameters: {{}}

EXECUTION RULES:
- If user intent matches a function, respond with: EXECUTE_FUNCTION: function_name | parameters_json
- If multiple functions needed, pick the most relevant one
- If no function needed, respond normally in Persian
- Always be conversational and helpful

Examples:
User: "یه وظیفه اضافه کن: خرید نان"
Response: EXECUTE_FUNCTION: add_task | {{"description": "خرید نان"}}

User: "وظیفه شماره 2 رو تمام کن"
Response: EXECUTE_FUNCTION: complete_task | {{"task_number": "2"}}

User: "30 دقیقه دیگر یادم بنداز قرار ملاقات دارم"
Response: EXECUTE_FUNCTION: set_reminder | {{"time": "30m", "description": "قرار ملاقات"}}

User: "چه خبر؟"
Response: (normal conversation in Persian)

Current context: {context if context else 'General conversation'}

Respond in Persian and be friendly and helpful."""

            # Prepare conversation history
            messages = [{"role": "system", "content": system_prompt}]
            
            # Add recent conversation history (last 3 messages)
            for msg in self.conversation_memory[user_id][-3:]:
                messages.append(msg)
            
            # Add current message
            messages.append({"role": "user", "content": message})

            # Get user's preferred model
            model = self.get_user_ai_model(user_id)

            headers = {
                "Authorization": f"Bearer {self.openrouter_api_key}",
                "Content-Type": "application/json",
            }

            data = {
                "model": model,
                "messages": messages,
                "max_tokens": 1000,
                "temperature": 0.7,
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{OPENROUTER_BASE_URL}/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        ai_response = result['choices'][0]['message']['content']
                        
                        # Check if AI wants to execute a function
                        if ai_response.startswith("EXECUTE_FUNCTION:"):
                            try:
                                # Parse function call
                                parts = ai_response.replace("EXECUTE_FUNCTION:", "").strip().split(" | ")
                                function_name = parts[0].strip()
                                parameters = json.loads(parts[1]) if len(parts) > 1 else {}
                                
                                # Execute the function
                                function_result = await self.execute_function(function_name, parameters, user_id)
                                
                                # Update conversation memory
                                self.conversation_memory[user_id].append({"role": "user", "content": message})
                                self.conversation_memory[user_id].append({"role": "assistant", "content": function_result})
                                
                                # Save to database
                                self.save_ai_conversation(user_id, message, function_result, model)
                                
                                return function_result
                                
                            except Exception as e:
                                logger.error(f"Function execution error: {e}")
                                return f"❌ خطا در اجرای دستور: {str(e)}"
                        
                        else:
                            # Normal conversation
                            # Update conversation memory
                            self.conversation_memory[user_id].append({"role": "user", "content": message})
                            self.conversation_memory[user_id].append({"role": "assistant", "content": ai_response})
                            
                            # Keep only last 6 messages in memory
                            if len(self.conversation_memory[user_id]) > 6:
                                self.conversation_memory[user_id] = self.conversation_memory[user_id][-6:]
                            
                            # Save to database
                            self.save_ai_conversation(user_id, message, ai_response, model)
                            
                            return ai_response
                    else:
                        error_text = await response.text()
                        logger.error(f"OpenRouter API error: {response.status} - {error_text}")
                        return f"🚫 خطا در دریافت پاسخ AI: {response.status}"

        except asyncio.TimeoutError:
            return "⏱️ زمان انتظار تمام شد. لطفاً دوباره تلاش کنید."
        except Exception as e:
            logger.error(f"AI response error: {e}")
            return f"🚫 خطا در ارتباط با AI: {str(e)}"

    def parse_time_string(self, time_str: str) -> datetime:
        """Parse time strings like '30m', '2h', '1d' into datetime"""
        # Clean the time string
        time_str = time_str.lower().strip()
        
        # Try to extract number and unit
        patterns = [
            r'(\d+)\s*(?:دقیقه|minute|min|m)',
            r'(\d+)\s*(?:ساعت|hour|h)',
            r'(\d+)\s*(?:روز|day|d)',
        ]
        
        for i, pattern in enumerate(patterns):
            match = re.search(pattern, time_str)
            if match:
                value = int(match.group(1))
                now = datetime.now()
                
                if i == 0:  # minutes
                    return now + timedelta(minutes=value)
                elif i == 1:  # hours
                    return now + timedelta(hours=value)
                elif i == 2:  # days
                    return now + timedelta(days=value)
        
        # Fallback: try basic format
        pattern = r'^(\d+)([mhd])$'
        match = re.match(pattern, time_str)
        
        if not match:
            raise ValueError("فرمت زمان: 30m, 2h, 1d یا '30 دقیقه'")
        
        value = int(match.group(1))
        unit = match.group(2)
        now = datetime.now()
        
        if unit == 'm':
            return now + timedelta(minutes=value)
        elif unit == 'h':
            return now + timedelta(hours=value)
        elif unit == 'd':
            return now + timedelta(days=value)

    async def send_reminder(self, user_id: int, description: str, reminder_id: int):
        """Send reminder notification"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("UPDATE reminders SET completed = TRUE WHERE id = ?", (reminder_id,))
            conn.commit()
            conn.close()
            
            message = f"⏰ **یادآوری:**\n🔔 {description}"
            await self.application.bot.send_message(chat_id=user_id, text=message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Error sending reminder: {e}")

    def save_ai_conversation(self, user_id: int, message: str, response: str, model: str):
        """Save AI conversation to database"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO ai_conversations (user_id, message, response, model_used) VALUES (?, ?, ?, ?)",
                (user_id, message, response, model)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error saving AI conversation: {e}")

    def get_user_ai_model(self, user_id: int) -> str:
        """Get user's preferred AI model"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT ai_model FROM settings WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result and result[0] else self.default_model
        except:
            return self.default_model

    def is_user_ai_enabled(self, user_id: int) -> bool:
        """Check if AI is enabled for user"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT ai_enabled FROM settings WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result is not None else True
        except:
            return True

    def set_user_ai_setting(self, user_id: int, enabled: bool):
        """Set user AI enabled/disabled"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO settings (user_id, ai_enabled) VALUES (?, ?)",
                (user_id, enabled)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error setting AI preference: {e}")

    def set_user_ai_model(self, user_id: int, model: str):
        """Set user's preferred AI model"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO settings (user_id, ai_model) VALUES (?, ?)",
                (user_id, model)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error setting AI model: {e}")

    # Command Handlers
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if self.is_owner(user_id):
            ai_status = "🤖 AI فعال" if self.ai_enabled else "🚫 AI غیرفعال"
            message = f"""🤖 **سلام! من جاروِیس هستم، دستیار هوشمند شما**

{ai_status}

✨ **قابلیت جدید: گفتگوی طبیعی!**
حالا می‌توانید بدون دستور خاص با من صحبت کنید:

💬 **مثال‌هایی از چیزهایی که می‌توانید بگویید:**
• "یه وظیفه اضافه کن: خرید نان"
• "وظایفم رو نشون بده"
• "وظیفه شماره 1 رو تمام کن"
• "یادداشت کن: فردا جلسه مهم دارم"
• "30 دقیقه دیگر یادم بنداز قرار دارم"
• "یه نکته آموزشی بگو"
• "انگیزه‌ام پایینه، یه جمله قشنگ بگو"
• "خلاصه امروزم رو بده"

🎯 **یا از دستورات سنتی استفاده کنید:**
• /addtask, /listtasks, /done
• /note, /mynotes, /remindme
• /learn, /quote, /summary

⚙️ **تنظیمات:** /ai, /model, /help

فقط پیام بفرستید و من متوجه منظورتان می‌شوم! 🚀"""
        else:
            message = "🤖 سلام! من جاروِیس هستم، اما فقط برای مالک خود کار می‌کنم."
        
        await update.message.reply_text(message, parse_mode='Markdown')

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_owner(update.effective_user.id):
            await update.message.reply_text("🚫 دسترسی محدود")
            return
        
        help_text = f"""🤖 **راهنمای کامل جاروِیس**

✨ **گفتگوی طبیعی (جدید!):**
فقط به زبان طبیعی بگویید چه می‌خواهید:

📋 **وظایف:**
• "یه وظیفه اضافه کن: متن وظیفه"
• "وظایفم رو نشون بده"
• "وظیفه شماره X رو تمام کن"

📝 **یادداشت:**
• "یادداشت کن: متن"
• "یادداشت‌هام رو نشون بده"

⏰ **یادآوری:**
• "30 دقیقه دیگر یادم بنداز: متن"
• "2 ساعت دیگر یادآوری بذار: متن"

🧠 **سایر موارد:**
• "یه نکته آموزشی بگو"
• "انگیزه‌ام کم شده، کمکم کن"
• "خلاصه امروزم رو بده"

🎯 **دستورات سنتی (اختیاری):**
• /addtask, /listtasks, /done
• /note, /mynotes, /remindme
• /learn, /quote, /summary

🤖 **تنظیمات AI:**
• /ai on/off - فعال/غیرفعال کردن
• /model - تغییر مدل AI
• /clear - پاک کردن حافظه گفتگو

⚙️ **سایر تنظیمات:**
• /offline, /online, /voice

AI فعال: {'✅' if self.ai_enabled else '❌'}
مدل فعال: {self.get_user_ai_model(self.owner_id)}

💡 **نکته:** حالا کافی است فقط بنویسید چه می‌خواهید!"""
        
        await update.message.reply_text(help_text, parse_mode='Markdown')

    async def ai_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle AI or show status"""
        if not self.is_owner(update.effective_user.id):
            return

        user_id = update.effective_user.id
        
        if context.args:
            setting = context.args[0].lower()
            if setting in ['on', 'enable', 'فعال']:
                self.set_user_ai_setting(user_id, True)
                message = "🤖 AI فعال شد! حالا می‌توانید با زبان طبیعی با من گفتگو کنید."
            elif setting in ['off', 'disable', 'غیرفعال']:
                self.set_user_ai_setting(user_id, False)
                message = "🚫 AI غیرفعال شد. فقط دستورات سنتی کار می‌کنند."
            else:
                message = "❌ استفاده: /ai on یا /ai off"
        else:
            ai_enabled = self.is_user_ai_enabled(user_id)
            model = self.get_user_ai_model(user_id)
            status = "فعال ✅" if ai_enabled else "غیرفعال ❌"
            message = f"🤖 **وضعیت AI:**\n\n📊 وضعیت: {status}\n🧠 مدل: {model}\n\n💡 برای تغییر: /ai on یا /ai off"
        
        await update.message.reply_text(message, parse_mode='Markdown')

    async def model_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Change AI model"""
        if not self.is_owner(update.effective_user.id):
            return

        user_id = update.effective_user.id
        
        if context.args:
            new_model = ' '.join(context.args)
            self.set_user_ai_model(user_id, new_model)
            message = f"🧠 مدل AI تغییر کرد به: {new_model}"
        else:
            current_model = self.get_user_ai_model(user_id)
            popular_models = [
                "anthropic/claude-3.5-sonnet",
                "openai/gpt-4-turbo",
                "openai/gpt-3.5-turbo",
                "google/gemini-pro",
                "meta-llama/llama-3-70b-instruct"
            ]
            
            models_text = "\n".join([f"• {model}" for model in popular_models])
            message = f"""🧠 **مدل فعلی:** {current_model}

**مدل‌های محبوب:**
{models_text}

💡 برای تغییر: /model <نام مدل>
مثال: /model openai/gpt-4-turbo"""
        
        await update.message.reply_text(message, parse_mode='Markdown')

    async def clear_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Clear conversation memory"""
        if not self.is_owner(update.effective_user.id):
            return

        user_id = update.effective_user.id
        if user_id in self.conversation_memory:
            del self.conversation_memory[user_id]
        
        await update.message.reply_text("🧹 حافظه گفتگو پاک شد! گفتگوی جدید شروع شد.")

    async def set_offline(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_owner(update.effective_user.id):
            return
        
        if context.args:
            self.offline_message = ' '.join(context.args)
        
        self.offline_mode = True
        message = f"📴 **حالت آفلاین فعال شد**\n\n💬 {self.offline_message}"
        await update.message.reply_text(message, parse_mode='Markdown')

    async def set_online(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_owner(update.effective_user.id):
            return
        
        self.offline_mode = False
        
        # Show offline messages
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT username, first_name, message, received_at FROM offline_messages ORDER BY received_at DESC LIMIT 10")
        messages = cursor.fetchall()
        cursor.execute("DELETE FROM offline_messages")
        conn.commit()
        conn.close()
        
        if messages:
            summary = "📱 **پیام‌های آفلاین:**\n\n"
            for username, first_name, message, received_at in messages:
                name = username or first_name or "ناشناس"
                time_str = datetime.fromisoformat(received_at).strftime('%m/%d %H:%M')
                summary += f"👤 {name} ({time_str}):\n💬 {message}\n\n"
            await update.message.reply_text(summary, parse_mode='Markdown')
        
        await update.message.reply_text("✅ **بازگشت به حالت آنلاین**\n\nجاروِیس آماده خدمت!", parse_mode='Markdown')

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle all text messages with natural language processing"""
        user_id = update.effective_user.id
        message_text = update.message.text
        
        if self.offline_mode and user_id != self.owner_id:
            user = update.effective_user
            
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO offline_messages (user_id, username, first_name, message) VALUES (?, ?, ?, ?)",
                          (user.id, user.username, user.first_name, message_text))
            conn.commit()
            conn.close()
            
            await update.message.reply_text(self.offline_message)
            
        elif user_id == self.owner_id:
            # Check if AI is enabled for this user
            if self.ai_enabled and self.is_user_ai_enabled(user_id):
                # Show typing indicator
                await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
                
                # Get AI response with natural language processing
                ai_response = await self.get_ai_response(user_id, message_text)
                await self.send_response(update, ai_response)
            else:
                await update.message.reply_text("👋 سلام! AI غیرفعال است. از دستورات استفاده کنید. /help برای راهنما\n\n💡 برای فعال کردن AI: /ai on")
        else:
            await update.message.reply_text("🤖 سلام! من جاروِیس هستم، اما فقط برای مالک خود کار می‌کنم.")

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == "clear_tasks_yes":
            user_id = query.from_user.id
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tasks WHERE user_id = ?", (user_id,))
            conn.commit()
            conn.close()
            await query.edit_message_text("🗑️ همه وظایف پاک شدند!")
        elif query.data == "clear_tasks_no":
            await query.edit_message_text("❌ عملیات لغو شد.")

    async def send_response(self, update: Update, message: str):
        """Send response to user"""
        try:
            await update.message.reply_text(message, parse_mode='Markdown')
        except Exception as e:
            # Fallback without markdown if parsing fails
            await update.message.reply_text(message)

    def setup_handlers(self):
        """Setup all command handlers"""
        handlers = [
            CommandHandler("start", self.start_command),
            CommandHandler("help", self.help_command),
            CommandHandler("ai", self.ai_command),
            CommandHandler("model", self.model_command),
            CommandHandler("clear", self.clear_conversation),
            CommandHandler("offline", self.set_offline),
            CommandHandler("online", self.set_online),
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message),
            CallbackQueryHandler(self.handle_callback),
        ]
        
        for handler in handlers:
            self.application.add_handler(handler)

    async def run(self):
        """Run the bot"""
        self.application = Application.builder().token(self.token).build()
        self.setup_handlers()
        
        # Start the scheduler
        self.scheduler.start()
        logger.info("Scheduler started")

        await self.application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

def main():
    """Main function"""
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Please set your BOT_TOKEN")
        return

    if OWNER_ID == 123456789:
        print("❌ Please set your OWNER_ID")
        return

    if OPENROUTER_API_KEY == "YOUR_OPENROUTER_API_KEY":
        print("⚠️  Warning: OpenRouter API key not set. AI features will be disabled.")
        print("   Get your API key from: https://openrouter.ai/")

    print("🚀 Starting Enhanced Jarvis with Natural Language AI...")

    try:
        bot = JarvisBot()
        
        if bot.ai_enabled:
            print("🤖 AI features: ENABLED")
            print("✨ Natural Language Processing: ENABLED")
            print(f"🧠 Default model: {bot.default_model}")
        else:
            print("🚫 AI features: DISABLED (API key not set)")
        
        print("✅ Enhanced Jarvis is ready! You can now use natural language.")
        print("💬 Try: 'یه وظیفه اضافه کن: خرید نان'")

        application = Application.builder().token(bot.token).build()
        bot.application = application
        bot.setup_handlers()
        bot.scheduler.start()
        logger.info("Scheduler started")

        # Use the bot's asyncio loop
        bot.loop.run_until_complete(application.initialize())
        bot.loop.run_until_complete(application.start())
        bot.loop.run_until_complete(application.updater.start_polling())
        bot.loop.run_forever()

    except KeyboardInterrupt:
        print("\n🛑 Bot stopped")
    except Exception as e:
        print(f"❌ Error: {e}")
        logger.error(f"Main error: {e}")

if __name__ == "__main__":
    print("🚀 Starting Enhanced Jarvis Bot...")
    
    try:
        bot = JarvisBot()
        
        if bot.ai_enabled:
            print("🤖 AI features: ENABLED")
            print("✨ Natural Language Processing: ENABLED")
            print(f"🧠 Default model: {bot.default_model}")
        else:
            print("🚫 AI features: DISABLED (API key not set)")
        
        print("✅ Enhanced Jarvis is ready!")
        
        # Run the bot
        bot.loop.run_until_complete(bot.run())
        
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped")
    except Exception as e:
        print(f"❌ Error: {e}")
        logging.error(f"Main error: {e}")