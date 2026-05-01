from telegram import Update, ForceReply,InlineKeyboardMarkup, InlineKeyboardButton, PhotoSize
import mysql.connector
from mysql.connector import Error

DB_HOST = "localhost"
DB_USER = "root"
DB_PASSWORD = ""
DB_NAME = "photohoarder"

async def debug_send(context, chat_id, text, debug):
    if debug:
        await context.bot.send_message(chat_id=chat_id, text=text)
        

async def link_sqlite(update: Update, file_id: str, drive_url: str) -> None:
    """Link file information to MySQL database"""
    try:
        conn = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
        )
        cursor = conn.cursor()
        
        chat_id = update.effective_chat.id
        chat_name = update.effective_chat.title or update.effective_user.first_name
        
        # Update or insert chat record with album link
        cursor.execute("""
            INSERT INTO chats (chat_id, chat_name, album_link)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE album_link = %s
        """, (chat_id, chat_name, drive_url, drive_url))
        
        conn.commit()
        cursor.close()
        conn.close()
        print(f"Linked file_id {file_id} to database for chat {chat_id}")
    except Error as e:
        print(f"Error linking to MySQL: {e}")