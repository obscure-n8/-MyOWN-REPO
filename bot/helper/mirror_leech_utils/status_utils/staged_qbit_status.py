from ...ext_utils.status_utils import (
    EngineStatus,
    MirrorStatus,
    get_raw_file_size,
    get_readable_file_size,
    get_readable_time,
)


class StagedQbitStatus:
    def __init__(self, listener, coordinator):
        self.listener = listener
        self.coordinator = coordinator
        self._engines = EngineStatus()

    @property
    def engine(self):
        if "Uploading" not in self.coordinator.phase:
            return self._engines.STATUS_QBIT
        if self.listener.is_leech:
            return self._engines.STATUS_TGRAM
        uploader = self.coordinator.active_uploader
        if self.listener.staged_drive_root or (
            uploader is not None and uploader.__class__.__name__ == "GoogleDriveUpload"
        ):
            return self._engines.STATUS_GDAPI
        return self._engines.STATUS_RCLONE

    async def status(self):
        return (
            MirrorStatus.STATUS_UPLOAD
            if "Uploading" in self.coordinator.phase
            else MirrorStatus.STATUS_DOWNLOAD
        )

    def name(self):
        done = self.coordinator.completed_files
        total = len(self.coordinator.files)
        return (
            f"[{self.coordinator.phase} | Files: {done}/{total}] {self.listener.name}"
        )

    def progress(self):
        total = self.coordinator.total_bytes or 1
        done = self.coordinator.completed_bytes + self._active_bytes()
        return f"{round(done * 100 / total, 2)}%"

    def _active_bytes(self):
        uploader = self.coordinator.active_uploader
        if uploader is None:
            return (
                0
                if "Uploading" in self.coordinator.phase
                else self.coordinator.current_bytes
            )
        if hasattr(uploader, "processed_bytes"):
            value = uploader.processed_bytes
            return int(value) if isinstance(value, (int, float)) else 0
        if hasattr(uploader, "transferred_size"):
            return get_raw_file_size(uploader.transferred_size)
        return 0

    def processed_bytes(self):
        return get_readable_file_size(
            self.coordinator.completed_bytes + self._active_bytes()
        )

    def size(self):
        return get_readable_file_size(self.coordinator.total_bytes)

    def speed(self):
        uploader = self.coordinator.active_uploader
        if uploader is not None and hasattr(uploader, "speed"):
            value = uploader.speed
            return (
                value
                if isinstance(value, str)
                else f"{get_readable_file_size(value)}/s"
            )
        return f"{get_readable_file_size(self.coordinator.speed)}/s"

    def eta(self):
        uploader = self.coordinator.active_uploader
        if uploader is not None and hasattr(uploader, "eta"):
            value = uploader.eta
            if isinstance(value, str):
                return value
        speed = self._active_speed()
        if speed <= 0:
            return "-"
        remaining = max(
            0,
            self.coordinator.total_bytes
            - self.coordinator.completed_bytes
            - self._active_bytes(),
        )
        return get_readable_time(remaining / speed)

    def _active_speed(self):
        uploader = self.coordinator.active_uploader
        if uploader is None or not hasattr(uploader, "speed"):
            return self.coordinator.speed
        value = uploader.speed
        return value if isinstance(value, (int, float)) else 0

    def gid(self):
        return self.coordinator.hash[:12]

    def task(self):
        return self

    async def cancel_task(self):
        await self.coordinator.cancel()

    def seeders_num(self):
        return getattr(self.coordinator, "num_seeds", 0)

    def leechers_num(self):
        return getattr(self.coordinator, "num_leechs", 0)
