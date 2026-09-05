from asyncio import Event, gather
from html import escape
from os import path as ospath
from time import time

from aiofiles.os import path as aiopath
from niquests import utils as rutils
from pyrogram.enums import ButtonStyle

from .. import (
    bot_loop,
    non_queued_dl,
    non_queued_up,
    queue_dict_lock,
    queued_dl,
    queued_up,
    task_dict,
    task_dict_lock,
)
from ..core.config_manager import Config
from ..helper.ext_utils.bot_utils import SetInterval, sync_to_async
from ..helper.ext_utils.db_handler import database
from ..helper.ext_utils.files_utils import clean_download, get_mime_type
from ..helper.ext_utils.links_utils import is_gdrive_id
from ..helper.ext_utils.status_utils import get_readable_file_size, get_readable_time
from ..helper.ext_utils.task_manager import start_from_queued
from ..helper.mirror_leech_utils.gdrive_utils.upload import GoogleDriveUpload
from ..helper.mirror_leech_utils.rclone_utils.transfer import RcloneTransferHelper
from ..helper.mirror_leech_utils.upload_utils.telegram_uploader import TelegramUploader
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_links,
    send_message,
    update_status_message,
)
from .mirror_leech import Mirror


class StagedMirror(Mirror):
    def __init__(self, client, message, is_leech=False):
        super().__init__(client, message, is_qbit=True, is_leech=is_leech)
        self.is_staged_qbit = True
        self.staged_coordinator = None
        self.staged_hash = ""
        self.staged_upload_error = ""
        self.staged_results = []
        self.staged_links = {}
        self.staged_drive_root = ""
        self._staged_upload_event = None

    async def on_upload_complete(
        self, link, files, folders, mime_type, rclone_path="", dir_id=""
    ):
        if self.staged_coordinator is None:
            return await super().on_upload_complete(
                link, files, folders, mime_type, rclone_path, dir_id
            )
        if self.is_leech and isinstance(files, dict):
            self.staged_links.update(files)
            if mime_type:
                self.staged_upload_error = f"Telegram could not upload {mime_type} file(s). The local batch was kept."
                if self._staged_upload_event:
                    self._staged_upload_event.set()
                return
        self.staged_results.append(
            (link, files, folders, mime_type, rclone_path, dir_id)
        )
        if isinstance(files, dict):
            self.staged_links.update(files)
        if dir_id and not self.staged_drive_root:
            self.staged_drive_root = dir_id
        if self._staged_upload_event:
            self._staged_upload_event.set()

    async def on_upload_error(self, error):
        if self.staged_coordinator is None:
            return await super().on_upload_error(error)
        self.staged_upload_error = str(error)
        if self._staged_upload_event:
            self._staged_upload_event.set()

    async def _drive_batch(self, stage_root):
        drive = GoogleDriveUpload(self, stage_root)
        self.staged_coordinator.active_uploader = drive
        drive.user_setting()
        drive.service = drive.authorize()
        if not self.staged_drive_root:
            self.staged_drive_root = drive.create_directory(self.name, self.up_dest)
        drive._updater = SetInterval(drive.update_interval, drive.progress)
        try:
            if await aiopath.isfile(stage_root):
                mime_type = await sync_to_async(get_mime_type, stage_root)
                result = await sync_to_async(
                    drive._upload_file,
                    stage_root,
                    ospath.basename(stage_root),
                    mime_type,
                    self.staged_drive_root,
                    True,
                )
                drive.total_files += 1
                result = self.staged_drive_root if result is None else result
            else:
                result = await sync_to_async(
                    drive._upload_dir, stage_root, self.staged_drive_root
                )
        finally:
            drive._updater.cancel()
        if result is None or self.is_cancelled:
            raise RuntimeError("Google Drive batch upload was cancelled.")
        self.staged_results.append(
            (
                drive.G_DRIVE_DIR_BASE_DOWNLOAD_URL.format(self.staged_drive_root),
                drive.total_files,
                drive.total_folders,
                "Folder",
                "",
                self.staged_drive_root,
            )
        )

    async def upload_staged_batch(self, stage_root, coordinator):
        self.staged_upload_error = ""
        self._staged_upload_event = Event()
        self.upload_start_time = time()
        self.size = sum(item.size for item in coordinator.current_batch)
        if self.is_leech:
            uploader = TelegramUploader(self, stage_root)
            coordinator.active_uploader = uploader
            await gather(update_status_message(self.message.chat.id), uploader.upload())
            await self._staged_upload_event.wait()
        elif is_gdrive_id(self.up_dest):
            await self._drive_batch(stage_root)
        elif self.up_dest == "mega:":
            raise ValueError("Mega uploads are not supported by staged torrents yet.")
        else:
            uploader = RcloneTransferHelper(self)
            coordinator.active_uploader = uploader
            await gather(
                update_status_message(self.message.chat.id), uploader.upload(stage_root)
            )
            await self._staged_upload_event.wait()

    async def staged_complete(self):
        total_files = len(self.staged_coordinator.files)
        msg = (
            f"<b><i>{escape(self.name)}</i></b>\n│"
            f"\n┟ <b>Task Size</b> → {get_readable_file_size(self.staged_coordinator.total_bytes)}"
            f"\n┠ <b>Files</b> → {total_files}"
            f"\n┠ <b>Mode</b> → Staged qBittorrent"
            f"\n┠ <b>Time Taken</b> → {get_readable_time(time() - self.message.date.timestamp())}"
        )
        button = None
        if self.is_leech and self.staged_links:
            lines = [msg, "\n<b>Files List:</b>"]
            for index, (link, name) in enumerate(self.staged_links.items(), 1):
                item_line = f"{index}. <a href='{link}'>{escape(name)}</a>"
                if (
                    sum(len(line) + 1 for line in lines)
                    + len(item_line)
                    + len(self.tag)
                    + 30
                    > 3900
                ):
                    lines.append(
                        f"...and {len(self.staged_links) - index + 1} more files."
                    )
                    break
                lines.append(item_line)
            msg = "\n".join(lines)
        elif self.staged_results and self.staged_results[0][0]:
            buttons = ButtonMaker()
            link = self.staged_results[0][0]
            if Config.SHOW_CLOUD_LINK:
                if "mega.nz" in link:
                    btn_label = "🔗 Mega Link"
                else:
                    btn_label = "☁️ Cloud Link"
                buttons.url_button(btn_label, link, style=ButtonStyle.PRIMARY)
            if self.staged_drive_root:
                INDEX_URL = self.user_dict.get("INDEX_URL", "") or ""
                if not INDEX_URL:
                    INDEX_URL = Config.INDEX_URL or ""
                if INDEX_URL and self.name:
                    safe_name = rutils.quote(self.name.strip("/"))
                    share_url = f"{INDEX_URL}/{safe_name}"
                    if self.staged_results[0][3] == "Folder":
                        share_url += "/"
                    buttons.url_button(
                        "⚡ Index Link", share_url, style=ButtonStyle.PRIMARY
                    )
                    if self.staged_results[0][3].startswith(
                        ("image", "video", "audio")
                    ):
                        buttons.url_button(
                            "🌐 View Link",
                            f"{share_url}?a=view",
                            style=ButtonStyle.PRIMARY,
                        )
            button = buttons.build_menu(2)
        msg += f"\n┃\n┖ <b>Task By</b> → {self.tag}"
        await send_message(self.message, msg, button)
        await clean_download(self.dir)
        await self._finish_staged_cleanup()

    async def staged_error(self, error, cleanup=False):
        uploaded = len(self.staged_links) or sum(
            result[1] for result in self.staged_results if isinstance(result[1], int)
        )
        suffix = f"\nAlready uploaded files: {uploaded}" if uploaded else ""
        await send_message(
            self.message, f"Staged torrent failed: {escape(error)}{suffix}"
        )
        if cleanup:
            await clean_download(self.dir)
        await self._finish_staged_cleanup()

    async def _finish_staged_cleanup(self):
        await clean_download(f"{self.dir}-stage")
        await delete_links(self.message)
        if Config.DATABASE_URL:
            await database.rm_complete_task(self.message.link)
        async with task_dict_lock:
            task_dict.pop(self.mid, None)
        async with queue_dict_lock:
            non_queued_dl.discard(self.mid)
            non_queued_up.discard(self.mid)
            queued_dl.pop(self.mid, None)
            queued_up.pop(self.mid, None)
        await start_from_queued()
        await update_status_message(self.message.chat.id)


async def qb_stream_mirror(client, message):
    bot_loop.create_task(StagedMirror(client, message).new_event())


async def qb_stream_leech(client, message):
    if Config.DISABLE_LEECH:
        await message.reply("The Leech command is currently disabled.")
        return
    bot_loop.create_task(StagedMirror(client, message, is_leech=True).new_event())
