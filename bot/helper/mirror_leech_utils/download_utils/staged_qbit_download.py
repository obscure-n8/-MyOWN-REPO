from asyncio import Event, gather, sleep
from contextlib import suppress
from os import link as hardlink
from pathlib import Path
from shutil import copyfile, disk_usage
from time import time

from aiofiles.os import makedirs, path as aiopath, remove
from aioqbt.api import AddFormBuilder, StopCondition

from .... import (
    DOWNLOAD_DIR,
    LOGGER,
    non_queued_up,
    queue_dict_lock,
    task_dict,
    task_dict_lock,
)
from ....core.config_manager import Config
from ....core.torrent_manager import TorrentManager
from ...ext_utils.bot_utils import sync_to_async
from ...ext_utils.db_handler import database
from ...ext_utils.files_utils import clean_download
from ...ext_utils.status_utils import get_readable_file_size
from ...ext_utils.task_manager import (
    check_blacklisted_keywords,
    check_running_tasks,
    limit_checker,
    start_from_queued,
    stop_duplicate_check,
)
from ...telegram_helper.message_utils import send_message, send_status_message
from ..status_utils.staged_qbit_status import StagedQbitStatus
from ..status_utils.qbit_status import QbittorrentStatus
from .staged_qbit_planner import StagedFile, plan_batch, preflight_error, usable_space


FILE_PRIORITY_SKIP = 0
FILE_PRIORITY_NORMAL = 1


async def get_qbit():
    await TorrentManager.ensure_qbit()
    if TorrentManager.qbittorrent is None:
        raise RuntimeError("qBittorrent client is unavailable.")
    return TorrentManager.qbittorrent


class StagedQbitCoordinator:
    def __init__(
        self, listener, torrent_hash: str, content_root: str, torrent_root_name: str
    ):
        self.listener = listener
        self.hash = torrent_hash
        self.content_root = content_root
        self.torrent_root_name = torrent_root_name
        self.files: list[StagedFile] = []
        self.pending: list[StagedFile] = []
        self.current_batch: list[StagedFile] = []
        self.completed_bytes = 0
        self.completed_files = 0
        self.total_bytes = 0
        self.phase = "Preparing"
        self.speed = 0
        self.num_seeds = 0
        self.num_leechs = 0
        self.current_bytes = 0
        self.cancelled = False
        self._queue_event = None
        self.active_uploader = None
        self.payload_started = False
        self._stage_dir = f"{self.listener.dir}-stage"

    async def _qbit(self):
        return await get_qbit()

    async def _ensure_torrent_added(self):
        qbt = await self._qbit()
        with suppress(Exception):
            info = await qbt.torrents.info(hashes=[self.hash])
            if info:
                return
        LOGGER.info(f"Re-adding missing torrent {self.hash} to qBittorrent...")
        form = AddFormBuilder.with_client(qbt)
        if await aiopath.exists(self.listener.link):
            from aiofiles import open as aiopen

            async with aiopen(self.listener.link, "rb") as torrent_file:
                form = form.include_file(await torrent_file.read())
        else:
            form = form.include_url(self.listener.link)
        form = form.savepath(self.listener.dir).tags([f"{self.listener.mid}"])
        with suppress(Exception):
            await qbt.torrents.add(form.build())
        for _ in range(20):
            with suppress(Exception):
                info = await qbt.torrents.info(hashes=[self.hash])
                if info and info[0].state not in ("metaDL", "checkingResumeData"):
                    break
            await sleep(1)

    async def cancel(self):
        self.cancelled = True
        self.listener.is_cancelled = True
        if self._queue_event is not None:
            self._queue_event.set()
        if self.active_uploader is not None and hasattr(
            self.active_uploader, "cancel_task"
        ):
            with suppress(Exception):
                await self.active_uploader.cancel_task()
        with suppress(Exception):
            qbt = await self._qbit()
            await qbt.torrents.stop([self.hash])
            await qbt.torrents.delete([self.hash], True)
            await qbt.torrents.delete_tags([f"{self.listener.mid}"])

    async def _free(self):
        return (await sync_to_async(disk_usage, DOWNLOAD_DIR)).free

    async def load_manifest(self):
        await self._ensure_torrent_added()
        qbt = await self._qbit()
        raw_files = []
        for _ in range(15):
            if self.cancelled or self.listener.is_cancelled:
                raise RuntimeError("Staged torrent was cancelled.")
            try:
                raw_files = await qbt.torrents.files(self.hash)
                if raw_files:
                    break
            except Exception:
                pass
            await sleep(1)
        if not raw_files:
            raise RuntimeError("Could not retrieve file list from qBittorrent.")
        selected = [
            StagedFile(f.index, f.name, f.size) for f in raw_files if f.priority != 0
        ]
        free = await self._free()
        budget = usable_space(free, Config.STAGED_TORRENT_STORAGE_PERCENT)
        max_upload_size = self.listener.max_split_size if self.listener.is_leech else 0
        if error := preflight_error(selected, budget, max_upload_size):
            if "safe free storage" in error and selected:
                largest = max(selected, key=lambda item: item.size)
                error = (
                    f"File '{largest.name}' ({get_readable_file_size(largest.size)}) "
                    f"is larger than safe free storage ({get_readable_file_size(budget)})."
                )
            elif "upload destination" in error:
                error += " Staged mode cannot split files because splitting needs extra storage."
            raise ValueError(error)
        self.files = selected
        self.total_bytes = sum(item.size for item in selected)
        self.listener.size = self.total_bytes

        if Config.DATABASE_URL:
            tasks_dict = await database.get_incomplete_tasks()
            chat_id = self.listener.message.chat.id
            tag = self.listener.tag
            link = self.listener.message.link
            completed_indices = set()
            if chat_id in tasks_dict and tag in tasks_dict[chat_id]:
                for task_entry in tasks_dict[chat_id][tag]:
                    if task_entry.get("link") == link or (
                        task_entry.get("command")
                        and task_entry.get("command") == self.listener.message.text
                    ):
                        completed_indices = set(
                            task_entry.get("staged_completed_indices", [])
                        )
                        if saved_root := task_entry.get("staged_drive_root"):
                            self.listener.staged_drive_root = saved_root
                        if saved_links := task_entry.get("staged_links"):
                            self.listener.staged_links.update(saved_links)
                        if saved_results := task_entry.get("staged_results"):
                            self.listener.staged_results = saved_results
                        if saved_bytes := task_entry.get("staged_completed_bytes"):
                            self.completed_bytes = saved_bytes
                            self.completed_files = len(completed_indices)
                        break
            if completed_indices:
                selected = [f for f in selected if f.index not in completed_indices]

        self.pending = selected.copy()
        if limit_message := await limit_checker(self.listener):
            raise ValueError(limit_message)

    async def _set_batch_priorities(self, batch: list[StagedFile]):
        qbt = await self._qbit()
        all_ids = [item.index for item in self.files]
        await qbt.torrents.file_prio(self.hash, all_ids, FILE_PRIORITY_SKIP)
        await qbt.torrents.file_prio(
            self.hash, [item.index for item in batch], FILE_PRIORITY_NORMAL
        )

    async def _wait_for_batch(self):
        while not self.cancelled and not self.listener.is_cancelled:
            try:
                await self._ensure_torrent_added()
                qbt = await self._qbit()
                info = await qbt.torrents.files(
                    self.hash, [item.index for item in self.current_batch]
                )
                with suppress(Exception):
                    tor_list = await qbt.torrents.info(hashes=[self.hash])
                    if tor_list:
                        tor = tor_list[0]
                        self.speed = tor.dlspeed
                        self.num_seeds = tor.num_seeds
                        self.num_leechs = tor.num_leechs
                self.current_bytes = sum(
                    int(item.size * item.progress) for item in info
                )
                if info and all(item.progress >= 1 for item in info):
                    return
            except Exception as e:
                LOGGER.debug(f"Staged batch poll error: {e}")
                with suppress(Exception):
                    await self._ensure_torrent_added()
                    await self._set_batch_priorities(self.current_batch)
                    qbt = await self._qbit()
                    await qbt.torrents.start([self.hash])
            await sleep(3)
        raise RuntimeError("Staged torrent was cancelled.")

    async def _make_stage_tree(self):
        await clean_download(self._stage_dir)
        root = Path(self._stage_dir) / self.torrent_root_name
        for item in self.current_batch:
            source = Path(self.content_root) / item.name
            target = Path(self._stage_dir) / item.name
            await makedirs(str(target.parent), exist_ok=True)
            try:
                await sync_to_async(hardlink, source, target)
            except OSError:
                await sync_to_async(copyfile, source, target)
        return str(root if root.exists() else Path(self._stage_dir))

    async def _delete_batch(self):
        failures = []
        for item in self.current_batch:
            path = f"{self.content_root}/{item.name}"
            if await aiopath.exists(path):
                try:
                    await remove(path)
                except Exception as error:
                    failures.append(f"{item.name}: {error}")
        await clean_download(self._stage_dir)
        if failures:
            raise RuntimeError(
                "Could not delete uploaded files: " + "; ".join(failures)
            )

    async def run(self):
        try:
            await self.load_manifest()
            async with task_dict_lock:
                task_dict[self.listener.mid] = StagedQbitStatus(self.listener, self)
            await send_status_message(self.listener.message)
            while self.pending and not self.listener.is_cancelled:
                budget = usable_space(
                    await self._free(), Config.STAGED_TORRENT_STORAGE_PERCENT
                )
                self.current_batch = plan_batch(self.pending, budget)
                if not self.current_batch:
                    raise RuntimeError(
                        "Available storage dropped below the size of every pending file."
                    )
                self.phase = "Downloading batch"
                self.current_bytes = 0
                await self._set_batch_priorities(self.current_batch)
                self.payload_started = True
                qbt = await self._qbit()
                await qbt.torrents.start([self.hash])
                await self._wait_for_batch()
                qbt = await self._qbit()
                await qbt.torrents.stop([self.hash])
                self.phase = "Uploading batch"
                upload_queued, upload_event = await check_running_tasks(
                    self.listener, "up"
                )
                await start_from_queued()
                if upload_queued:
                    self._queue_event = upload_event
                    await upload_event.wait()
                    if self.listener.is_cancelled:
                        raise RuntimeError("Staged torrent was cancelled.")
                    self._queue_event = None
                stage_root = await self._make_stage_tree()
                await self.listener.upload_staged_batch(stage_root, self)
                if self.listener.staged_upload_error:
                    raise RuntimeError(self.listener.staged_upload_error)
                await self._delete_batch()
                batch_bytes = sum(item.size for item in self.current_batch)
                self.completed_bytes += batch_bytes
                self.completed_files += len(self.current_batch)
                self.active_uploader = None
                batch_ids = {item.index for item in self.current_batch}
                self.pending = [
                    item for item in self.pending if item.index not in batch_ids
                ]
                self.current_batch = []
                if Config.DATABASE_URL:
                    all_completed = {
                        f.index for f in self.files if f not in self.pending
                    }
                    await database.update_staged_task_progress(
                        self.listener.message.link,
                        completed_indices=all_completed,
                        staged_drive_root=self.listener.staged_drive_root,
                        staged_links=self.listener.staged_links,
                        staged_results=self.listener.staged_results,
                        completed_bytes=self.completed_bytes,
                    )
                async with queue_dict_lock:
                    non_queued_up.discard(self.listener.mid)
                await start_from_queued()
                if self.pending:
                    download_queued, download_event = await check_running_tasks(
                        self.listener, "dl"
                    )
                    if download_queued:
                        self.phase = "Queued for next batch"
                        self._queue_event = download_event
                        await download_event.wait()
                        if self.listener.is_cancelled:
                            raise RuntimeError("Staged torrent was cancelled.")
                        self._queue_event = None
            if self.listener.is_cancelled:
                raise RuntimeError("Staged torrent was cancelled.")
            self.phase = "Finalizing"
            with suppress(Exception):
                qbt = await self._qbit()
                await qbt.torrents.delete([self.hash], True)
                await qbt.torrents.delete_tags([f"{self.listener.mid}"])
            await self.listener.staged_complete()
        except Exception as error:
            LOGGER.error(f"Staged torrent failed: {error}")
            clean_local = True
            with suppress(Exception):
                qbt = await self._qbit()
                await gather(
                    qbt.torrents.stop([self.hash]),
                    qbt.torrents.delete([self.hash], clean_local),
                    qbt.torrents.delete_tags([f"{self.listener.mid}"]),
                    return_exceptions=True,
                )
            await self.listener.staged_error(
                "Stopped by user!" if self.listener.is_cancelled else str(error),
                cleanup=clean_local,
            )


async def add_staged_qb_torrent(listener, path):
    if Config.DISABLE_TORRENTS:
        await listener.on_download_error("Torrents are disabled in the configuration.")
        return
    is_bl, bl_kw = await check_blacklisted_keywords(
        listener, listener.name or listener.link
    )
    if is_bl:
        await listener.on_download_error(
            f"Task cancelled! Name/Link contains blacklisted keyword: <code>{bl_kw}</code>"
        )
        return
    if listener.name:
        msg, button = await stop_duplicate_check(listener)
        if msg:
            await listener.on_download_error(msg, button)
            return
    try:
        qbt = await get_qbit()
        form = AddFormBuilder.with_client(qbt)
        if await aiopath.exists(listener.link):
            from aiofiles import open as aiopen

            async with aiopen(listener.link, "rb") as torrent_file:
                form = form.include_file(await torrent_file.read())
        else:
            form = form.include_url(listener.link)
        add_to_queue, event = await check_running_tasks(listener)
        form = (
            form.savepath(path)
            .tags([f"{listener.mid}"])
            .stopped(add_to_queue)
            .stop_condition(StopCondition.METADATA_RECEIVED)
        )
        await qbt.torrents.add(form.build())
        info = []
        while not info:
            if listener.is_cancelled:
                return
            qbt = await get_qbit()
            info = await qbt.torrents.info(tag=f"{listener.mid}")
            await sleep(1)
    except Exception as error:
        await listener.on_download_error(f"Unable to add staged torrent: {error}")
        return
    torrent = info[0]
    listener.name = listener.name or torrent.name
    listener.staged_hash = torrent.hash
    async with task_dict_lock:
        task_dict[listener.mid] = QbittorrentStatus(listener, queued=add_to_queue)
    await listener.on_download_start()
    if add_to_queue:
        await event.wait()
        if listener.is_cancelled:
            return
        qbt = await get_qbit()
        await qbt.torrents.start([torrent.hash])
    metadata_started = time()
    while True:
        if listener.is_cancelled:
            return
        qbt = await get_qbit()
        info = await qbt.torrents.info(hashes=[torrent.hash])
        if not info:
            return
        torrent = info[0]
        if torrent.state not in ("metaDL", "checkingResumeData"):
            break
        if (
            Config.TORRENT_TIMEOUT
            and time() - metadata_started >= Config.TORRENT_TIMEOUT
        ):
            await qbt.torrents.delete([torrent.hash], True)
            await listener.on_download_error("Torrent metadata timed out.")
            return
        await sleep(1)
    qbt = await get_qbit()
    await qbt.torrents.stop([torrent.hash])
    listener.name = listener.name or torrent.name
    msg, button = await stop_duplicate_check(listener)
    if msg:
        await sleep(0.3)
        await qbt.torrents.delete([torrent.hash], True)
        await qbt.torrents.delete_tags([f"{listener.mid}"])
        await listener.on_download_error(msg, button)
        return
    is_bl, bl_kw = await check_blacklisted_keywords(listener, listener.name)
    if is_bl:
        await sleep(0.3)
        await qbt.torrents.delete([torrent.hash], True)
        await qbt.torrents.delete_tags([f"{listener.mid}"])
        await listener.on_download_error(
            f"Task cancelled! Name contains blacklisted keyword: <code>{bl_kw}</code>"
        )
        return
    if listener.select:
        from ...ext_utils.bot_utils import bt_selection_buttons

        listener.staged_selection_event = Event()
        await send_message(
            listener.message,
            "<b>Download Paused!</b>\n\nSelect files and press <b>Done Selecting</b> to start staged downloading.",
            bt_selection_buttons(torrent.hash),
        )
        await listener.staged_selection_event.wait()
        if listener.is_cancelled:
            return
    content_root = torrent.content_path.rsplit("/", 1)[0]
    torrent_root_name = torrent.content_path.rsplit("/", 1)[-1]
    coordinator = StagedQbitCoordinator(
        listener, torrent.hash, content_root, torrent_root_name
    )
    listener.staged_coordinator = coordinator
    await coordinator.run()
