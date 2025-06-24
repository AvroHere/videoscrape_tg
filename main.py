import os
import logging
from typing import Optional, List, Dict
from pathlib import Path
import tempfile
from datetime import datetime
import asyncio

from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot configuration
TOKEN = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
ADMIN_UID = xxxxxxxxxx  # Only respond to this user ID
TARGET_GROUP_ID = xxxxxxxxxxx  # Group to send videos to
MAX_VIDEO_SIZE = 49 * 1024 * 1024  # 49MB (Telegram file size limit for bots)
TEMP_DIR = Path(tempfile.gettempdir()) / "telegram_video_bot"
TEMP_DIR.mkdir(exist_ok=True, parents=True)

# Global variable to store remaining links and status messages
current_batch = {
    'all_links': [],
    'processed': 0,
    'failed': [],
    'remaining': [],
    'start_time': None,
    'user_id': None,
    'last_processed_link': None,
    'is_processing': False,
    'is_paused': False,
    'status_message_id': None,  # To track the status message for updates
    'last_auto_send': 0,  # Track when we last sent the auto remain file
    'caption_settings': {
        'active': False,
        'remaining': 0,
        'text': None
    }
}

class VideoDownloader:
    @staticmethod
    async def check_video_size(url: str) -> bool:
        """Check if video size exceeds limit before downloading."""
        try:
            from yt_dlp import YoutubeDL
            
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'extract_flat': True,
            }
            
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and 'filesize' in info:
                    return info['filesize'] <= MAX_VIDEO_SIZE
                return True  # Proceed if size can't be determined
        except Exception as e:
            logger.warning(f"Couldn't check video size: {e}")
            return True  # Proceed if check fails

    @staticmethod
    async def get_highest_quality_video(url: str) -> Optional[Path]:
        """Download the highest quality video from the provided URL."""
        try:
            # First check video size
            if not await VideoDownloader.check_video_size(url):
                logger.info(f"Video exceeds size limit: {url}")
                return None

            # Try youtube-dlp first
            try:
                from yt_dlp import YoutubeDL
                
                ydl_opts = {
                    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                    'outtmpl': str(TEMP_DIR / '%(id)s.%(ext)s'),
                    'quiet': True,
                    'no_warnings': True,
                    'merge_output_format': 'mp4',
                    'max_filesize': MAX_VIDEO_SIZE,
                }
                
                with YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if info:
                        filename = ydl.prepare_filename(info)
                        downloaded_file = Path(filename)
                        
                        # Double-check file size after download
                        if downloaded_file.stat().st_size > MAX_VIDEO_SIZE:
                            os.unlink(downloaded_file)
                            return None
                        return downloaded_file
            except ImportError:
                logger.warning("yt-dlp not available, trying pytube")
                
                # Fallback to pytube for YouTube links
                from pytube import YouTube
                if "youtube.com" in url or "youtu.be" in url:
                    yt = YouTube(url)
                    stream = yt.streams.filter(
                        progressive=True, 
                        file_extension='mp4'
                    ).order_by('resolution').desc().first()
                    
                    if stream:
                        # Check size before downloading
                        if stream.filesize > MAX_VIDEO_SIZE:
                            return None
                            
                        output_path = str(TEMP_DIR / f"{yt.video_id}.mp4")
                        stream.download(output_path=output_path)
                        downloaded_file = Path(output_path) / stream.default_filename
                        
                        # Final size check
                        if downloaded_file.stat().st_size > MAX_VIDEO_SIZE:
                            os.unlink(downloaded_file)
                            return None
                        return downloaded_file
            
            return None
        except Exception as e:
            logger.error(f"Error downloading video: {e}")
            return None

class TelegramBot:
    def __init__(self, token: str):
        self.application = Application.builder().token(token).build()
        self._setup_handlers()
        self.processing_task = None

    def _setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("help", self.help))
        self.application.add_handler(CommandHandler("remain", self.show_remain))
        self.application.add_handler(CommandHandler("stopnow", self.pause_processing))
        self.application.add_handler(CommandHandler("startnow", self.resume_processing))
        self.application.add_handler(CommandHandler("clean", self.clear_queue))
        self.application.add_handler(CommandHandler("skip", self.skip_links))
        self.application.add_handler(CommandHandler("cap", self.set_caption))
        self.application.add_handler(MessageHandler(
            filters.TEXT | filters.Document.TXT, 
            self.handle_message
        ))

    async def is_admin(self, update: Update) -> bool:
        """Check if the message is from the admin."""
        return update.effective_user.id == ADMIN_UID

    async def update_status_message(self, context: ContextTypes.DEFAULT_TYPE, status_text: str) -> None:
        """Update or send the status message."""
        try:
            if current_batch['status_message_id']:
                await context.bot.edit_message_text(
                    chat_id=current_batch['user_id'],
                    message_id=current_batch['status_message_id'],
                    text=status_text
                )
            else:
                message = await context.bot.send_message(
                    chat_id=current_batch['user_id'],
                    text=status_text
                )
                current_batch['status_message_id'] = message.message_id
        except Exception as e:
            logger.error(f"Error updating status message: {e}")

    async def send_completion_notification(self, context: ContextTypes.DEFAULT_TYPE, url: str, success: bool, error_msg: str = "") -> None:
        """Send notification when a link is fully processed."""
        processed = current_batch['processed']
        total = len(current_batch['all_links'])
        remain = total - processed
        
        status_emoji = "✅" if success else "❌"
        error_text = f"\n⚠️ Error: {error_msg}" if error_msg else ""
        
        completion_text = (
            f"### {status_emoji} Processed: {processed}/{total}\n"
            f"🔗 **Processed Link:**\n"
            f"`{url}`\n"
            f"📉 **Remaining:** {remain}"
            f"{error_text}"
        )
        
        try:
            # Send new completion message
            await context.bot.send_message(
                chat_id=current_batch['user_id'],
                text=completion_text
            )
            
            # Clear the status message ID to start fresh for next link
            current_batch['status_message_id'] = None
            
            # Check if we should send remaining links after every 5 processed links
            if processed > 0 and processed % 5 == 0:
                await self.send_remain_links(context)
        except Exception as e:
            logger.error(f"Error sending completion notification: {e}")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send welcome message when command /start is issued."""
        if not await self.is_admin(update):
            return
            
        welcome_text = """
🤖 Welcome to Video Downloader Bot!

I can download videos from various platforms and send them to your group.

🔹 Send me a video URL to download
🔹 Upload a .txt file with multiple links (one per line)
🔹 I'll automatically send videos to the target group

Commands:
/start - Show this welcome message
/help - Show all available commands
/remain - Get remaining links as text file
/stopnow - Pause processing
/startnow - Resume processing
/clean - Clear all queued links
/skip N - Skip N links from current batch
/cap N <Caption> - Add caption to next N videos
"""
        await update.message.reply_text(welcome_text)

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send help message when command /help is issued."""
        if not await self.is_admin(update):
            return
            
        help_text = """
📋 Admin Commands:

/start - Show welcome message
/help - Show this help message
/remain - Get remaining links as text file
/stopnow - Pause processing
/startnow - Resume processing
/clean - Clear all queued links
/skip N - Skip N links from current batch
/cap N <Caption> - Add caption to next N videos

How to use:
1. Send a single video URL to download
2. Or upload a .txt file with multiple links
3. I'll send videos to target group automatically
4. You'll receive progress updates after each video
"""
        await update.message.reply_text(help_text)

    async def set_caption(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set a caption for the next N videos with /cap N <Caption Here> command."""
        if not await self.is_admin(update):
            return
        
        if not context.args or len(context.args) < 2 or not context.args[0].isdigit():
            await update.message.reply_text(
                "Usage: /cap N <Caption Here>\n"
                "Example: /cap 5 Check out this video!\n"
                "This will add the caption to the next 5 videos."
            )
            return
        
        count = int(context.args[0])
        if count <= 0:
            await update.message.reply_text("Please provide a positive number of videos to caption.")
            return
        
        caption_text = ' '.join(context.args[1:])
        
        current_batch['caption_settings'] = {
            'active': True,
            'remaining': count,
            'text': caption_text
        }
        
        await update.message.reply_text(
            f"📝 Caption set for next {count} videos:\n"
            f"\"{caption_text}\"\n\n"
            f"After {count} videos, captions will be automatically disabled."
        )

    async def skip_links(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Skip N links from current batch with /skip N command."""
        if not await self.is_admin(update):
            return
        
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text("Usage: /skip N (where N is number of links to skip)")
            return
        
        skip_count = int(context.args[0])
        if skip_count <= 0:
            await update.message.reply_text("Please provide a positive number of links to skip.")
            return
        
        if not current_batch['is_processing']:
            await update.message.reply_text("No batch processing in progress to skip links from.")
            return
        
        if skip_count >= len(current_batch['remaining']):
            await update.message.reply_text(f"Skip count {skip_count} is greater than remaining links ({len(current_batch['remaining'])}). Cancelling batch.")
            await self.clear_queue(update, context)
            return
        
        # Cancel current processing task
        if self.processing_task and not self.processing_task.done():
            self.processing_task.cancel()
            try:
                await self.processing_task
            except asyncio.CancelledError:
                pass
        
        # Skip N links
        skipped_links = current_batch['remaining'][:skip_count]
        current_batch['remaining'] = current_batch['remaining'][skip_count:]
        current_batch['processed'] += skip_count
        
        # Create skipped links file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        skipped_file = TEMP_DIR / f"skipped_links_{skip_count}_{timestamp}.txt"
        with open(skipped_file, 'w') as f:
            f.write('\n'.join(skipped_links))
        
        # Send the file
        with open(skipped_file, 'rb') as f:
            await update.message.reply_document(
                document=InputFile(f),
                caption=f"⏭️ Skipped {skip_count} links!\n"
                       f"📊 New progress: {current_batch['processed']}/{len(current_batch['all_links'])}\n"
                       f"⏳ Remaining: {len(current_batch['remaining'])}",
            )
        
        # Clean up
        os.unlink(skipped_file)
        
        # Restart processing with remaining links
        current_batch['is_processing'] = True
        self.processing_task = asyncio.create_task(self.process_batch(context))
        
        await update.message.reply_text(f"▶️ Resumed processing from link {skip_count+1}")

    async def show_remain(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show remaining links when /remain command is sent."""
        if not await self.is_admin(update):
            return
        
        # Don't check if batch is in progress - just send whatever is in current_batch
        remain_count = len(current_batch['remaining'])
        processed = current_batch['processed']
        total = len(current_batch['all_links'])
        
        if remain_count == 0:
            await update.message.reply_text("No remaining links to process.")
            return
        
        # Create remain links file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        remain_file = TEMP_DIR / f"remain_links_{remain_count}_{timestamp}.txt"
        with open(remain_file, 'w') as f:
            f.write('\n'.join(current_batch['remaining']))
        
        # Send the file immediately
        with open(remain_file, 'rb') as f:
            await update.message.reply_document(
                document=InputFile(f),
                caption=f"📊 Progress: {processed}/{total}\n"
                       f"⏳ Remaining: {remain_count}\n"
                       f"🔗 Last processed: {current_batch['last_processed_link'] if current_batch['last_processed_link'] else 'None'}",
            )
        
        # Clean up
        os.unlink(remain_file)

    async def pause_processing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pause current processing with /stopnow command."""
        if not await self.is_admin(update):
            return
        
        if not current_batch['is_processing']:
            await update.message.reply_text("No processing in progress to pause.")
            return
        
        if current_batch['is_paused']:
            await update.message.reply_text("Processing is already paused.")
            return
        
        current_batch['is_paused'] = True
        await self.update_status_message(
            context,
            f"⏸️ Processing paused at {current_batch['processed']}/{len(current_batch['all_links'])}\n"
            f"Last link: {current_batch['last_processed_link']}\n"
            f"Use /startnow to resume."
        )
        await update.message.reply_text("⏸️ Processing paused. Use /startnow to resume.")

    async def resume_processing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Resume paused processing with /startnow command."""
        if not await self.is_admin(update):
            return
        
        if not current_batch['is_paused']:
            await update.message.reply_text("Processing is not paused.")
            return
        
        current_batch['is_paused'] = False
        
        # Restart processing if it was stopped
        if not current_batch['is_processing']:
            current_batch['is_processing'] = True
            self.processing_task = asyncio.create_task(self.process_batch(context))
        
        await self.update_status_message(
            context,
            f"▶️ Resuming processing...\n"
            f"Current progress: {current_batch['processed']}/{len(current_batch['all_links'])}\n"
            f"Next link: {current_batch['remaining'][0] if current_batch['remaining'] else 'None'}"
        )
        await update.message.reply_text("▶️ Processing resumed!")

    async def clear_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Clear all queued links with /clean command."""
        if not await self.is_admin(update):
            return
        
        if not current_batch['remaining']:
            await update.message.reply_text("Queue is already empty.")
            return
        
        # Cancel any ongoing processing
        if self.processing_task and not self.processing_task.done():
            self.processing_task.cancel()
            try:
                await self.processing_task
            except asyncio.CancelledError:
                pass
        
        count = len(current_batch['remaining'])
        current_batch.update({
            'all_links': [],
            'processed': 0,
            'failed': [],
            'remaining': [],
            'start_time': None,
            'user_id': None,
            'last_processed_link': None,
            'is_processing': False,
            'is_paused': False,
            'status_message_id': None,
            'last_auto_send': 0,
            'caption_settings': {
                'active': False,
                'remaining': 0,
                'text': None
            }
        })
        await update.message.reply_text(f"🧹 Cleared {count} links from queue.")

    async def send_remain_links(self, context: ContextTypes.DEFAULT_TYPE):
        """Send remaining links file to user automatically after every 5 links."""
        if not current_batch['remaining'] or not current_batch['user_id']:
            return
        
        remain_count = len(current_batch['remaining'])
        processed = current_batch['processed']
        total = len(current_batch['all_links'])
        
        # Only send if we've processed at least 5 more links since last auto-send
        if processed - current_batch['last_auto_send'] < 5:
            return
        
        # Update last auto-send counter
        current_batch['last_auto_send'] = processed
        
        # Create remain links file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        remain_file = TEMP_DIR / f"remain_links_{remain_count}_{timestamp}.txt"
        with open(remain_file, 'w') as f:
            f.write('\n'.join(current_batch['remaining']))
        
        # Send the file
        with open(remain_file, 'rb') as f:
            await context.bot.send_document(
                chat_id=current_batch['user_id'],
                document=InputFile(f),
                caption=f"📦 Automatic update after {processed % 5 if processed % 5 != 0 else 5} links processed\n"
                       f"📊 Progress: {processed}/{total}\n"
                       f"⏳ Remaining: {remain_count}\n"
                       f"🔗 Last processed: {current_batch['last_processed_link']}",
            )
        
        # Clean up
        os.unlink(remain_file)

    async def process_single_link(self, url: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Process a single video link and send to target group."""
        video_path = None
        error_msg = ""
        
        try:
            current_batch['last_processed_link'] = url
            processed = current_batch['processed'] + 1
            total = len(current_batch['all_links'])
            
            # Initial status update
            await self.update_status_message(
                context,
                f"### ⏳ Processing: {processed}/{total}\n"
                f"📎 Link: `{url}`\n"
                f"📊 **Status Updates (Live):**\n"
                f"- 🔽 Downloading..."
            )
            
            # Download the video
            video_path = await VideoDownloader.get_highest_quality_video(url)
            
            if not video_path or not video_path.exists():
                if video_path is None:  # Specifically for size limit exceeded
                    error_msg = "Video exceeds 49MB limit"
                    await self.update_status_message(
                        context,
                        f"### ⏳ Processing: {processed}/{total}\n"
                        f"📎 Link: `{url}`\n"
                        f"📊 **Status Updates (Live):**\n"
                        f"- ⚠️ Error: {error_msg}"
                    )
                else:
                    error_msg = "Download failed"
                    await self.update_status_message(
                        context,
                        f"### ⏳ Processing: {processed}/{total}\n"
                        f"📎 Link: `{url}`\n"
                        f"📊 **Status Updates (Live):**\n"
                        f"- ⚠️ Error: {error_msg}"
                    )
                
                current_batch['failed'].append(url)
                await self.send_completion_notification(context, url, False, error_msg)
                return False
            
            # Update status - downloaded
            await self.update_status_message(
                context,
                f"### ⏳ Processing: {processed}/{total}\n"
                f"📎 Link: `{url}`\n"
                f"📊 **Status Updates (Live):**\n"
                f"- ✅ Downloaded\n"
                f"- 🛠️ Preparing for upload"
            )
            
            # Send the video to target group
            with open(video_path, 'rb') as video_file:
                await self.update_status_message(
                    context,
                    f"### ⏳ Processing: {processed}/{total}\n"
                    f"📎 Link: `{url}`\n"
                    f"📊 **Status Updates (Live):**\n"
                    f"- ✅ Downloaded\n"
                    f"- 📤 Uploading..."
                )
                
                # Handle caption settings
                caption = None
                if current_batch['caption_settings']['active']:
                    caption = current_batch['caption_settings']['text']
                    current_batch['caption_settings']['remaining'] -= 1
                    
                    if current_batch['caption_settings']['remaining'] <= 0:
                        current_batch['caption_settings']['active'] = False
                        await context.bot.send_message(
                            chat_id=current_batch['user_id'],
                            text="ℹ️ Caption mode has been automatically disabled as the specified number of videos have been processed."
                        )

                await context.bot.send_video(
                    chat_id=TARGET_GROUP_ID,
                    video=video_file,
                    supports_streaming=True,
                    caption=caption,
                    width=1280,
                    height=720,
                    write_timeout=120,
                    read_timeout=120
                )
            
            current_batch['processed'] += 1
            await self.send_completion_notification(context, url, True)
            return True
            
        except Exception as e:
            logger.error(f"Error processing video {url}: {e}")
            error_msg = str(e)
            await self.update_status_message(
                context,
                f"### ⏳ Processing: {processed}/{total}\n"
                f"📎 Link: `{url}`\n"
                f"📊 **Status Updates (Live):**\n"
                f"- ⚠️ Error: {error_msg}"
            )
            current_batch['failed'].append(url)
            await self.send_completion_notification(context, url, False, error_msg)
            return False
        finally:
            # Clean up
            if video_path and video_path.exists():
                try:
                    os.unlink(video_path)
                except Exception as e:
                    logger.error(f"Error deleting video file: {e}")

    async def process_batch(self, context: ContextTypes.DEFAULT_TYPE):
        """Process all links in the current batch."""
        current_batch['is_processing'] = True
        current_batch['start_time'] = datetime.now()
        
        try:
            while current_batch['remaining']:
                # Check for pause
                while current_batch['is_paused']:
                    await asyncio.sleep(1)
                    continue
                
                # Check for cancellation
                if not current_batch['is_processing']:
                    break
                
                url = current_batch['remaining'][0]
                if not (url.startswith('http://') or url.startswith('https://')):
                    current_batch['remaining'].remove(url)
                    continue
                
                success = await self.process_single_link(url, context)
                current_batch['remaining'].remove(url)
        
            # Final report if processing wasn't cancelled
            if current_batch['is_processing']:
                elapsed = datetime.now() - current_batch['start_time']
                await context.bot.send_message(
                    chat_id=current_batch['user_id'],
                    text=f"🎉 Batch processing completed!\n\n"
                         f"✅ Success: {current_batch['processed']}\n"
                         f"❌ Failed: {len(current_batch['failed'])}\n"
                         f"⏱️ Time taken: {elapsed}"
                )
        except asyncio.CancelledError:
            logger.info("Batch processing was cancelled")
        except Exception as e:
            logger.error(f"Error during batch processing: {e}")
            await context.bot.send_message(
                chat_id=current_batch['user_id'],
                text=f"❌ Error during batch processing: {e}"
            )
        finally:
            current_batch['is_processing'] = False
            current_batch['status_message_id'] = None

    async def add_links_to_queue(self, links: List[str], update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add new links to the processing queue."""
        # Initialize batch if empty
        if not current_batch['all_links']:
            current_batch.update({
                'all_links': links,
                'processed': 0,
                'failed': [],
                'remaining': links.copy(),
                'user_id': update.effective_user.id,
                'last_processed_link': None,
                'is_processing': True,
                'is_paused': False,
                'status_message_id': None,
                'last_auto_send': 0,
                'caption_settings': {
                    'active': False,
                    'remaining': 0,
                    'text': None
                }
            })
            
            # Start processing if not already running
            if not self.processing_task or self.processing_task.done():
                self.processing_task = asyncio.create_task(self.process_batch(context))
            
            await update.message.reply_text(
                f"📦 Starting batch processing of {len(links)} links...\n"
                f"First link will be processed shortly.\n"
                f"Use /remain to check progress or /clean to stop."
            )
        else:
            # Add new links to existing queue
            current_batch['all_links'].extend(links)
            current_batch['remaining'].extend(links)
            
            total_links = len(current_batch['all_links'])
            new_links_count = len(links)
            
            await update.message.reply_text(
                f"➕ {new_links_count} links added successfully!\n"
                f"📊 Total in queue: {total_links}\n"
                f"⏳ Currently processing: {current_batch['processed'] + 1}/{total_links}"
            )
            
            # Restart processing if it was completed
            if not current_batch['is_processing'] and current_batch['remaining']:
                current_batch['is_processing'] = True
                self.processing_task = asyncio.create_task(self.process_batch(context))

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle incoming messages containing URLs or text files."""
        if not await self.is_admin(update):
            return
        
        message = update.message
        
        # Handle text file with multiple links
        if message.document and message.document.mime_type == 'text/plain':
            try:
                # Download the text file
                file = await message.document.get_file()
                temp_file = TEMP_DIR / "links.txt"
                await file.download_to_drive(temp_file)
                
                # Read links from file
                with open(temp_file, 'r') as f:
                    links = [line.strip() for line in f.readlines() if line.strip()]
                
                # Add links to queue
                await self.add_links_to_queue(links, update, context)
                
                # Clean up
                os.unlink(temp_file)
                
            except Exception as e:
                logger.error(f"Error processing text file: {e}")
                await message.reply_text(f"❌ Error processing text file: {e}")
        
        # Handle single link
        elif message.text and (message.text.startswith('http://') or message.text.startswith('https://')):
            url = message.text.strip()
            await self.add_links_to_queue([url], update, context)

    def run(self, use_webhook: bool = False):
        """Run the bot."""
        if use_webhook:
            # Webhook configuration would go here
            pass
        else:
            self.application.run_polling()

if __name__ == "__main__":
    bot = TelegramBot(TOKEN)
    bot.run()
