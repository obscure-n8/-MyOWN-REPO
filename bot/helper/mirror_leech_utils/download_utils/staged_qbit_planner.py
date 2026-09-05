from dataclasses import dataclass


DEFAULT_STORAGE_PERCENT = 50


@dataclass(frozen=True)
class StagedFile:
    index: int
    name: str
    size: int


def normalize_storage_percent(percent, default: int = DEFAULT_STORAGE_PERCENT) -> int:
    try:
        percent = int(percent)
    except (TypeError, ValueError):
        return default
    if percent <= 0:
        return default
    return min(percent, 100)


def usable_space(free: int, percent: int = DEFAULT_STORAGE_PERCENT) -> int:
    return max(0, free) * normalize_storage_percent(percent) // 100


def plan_batch(files: list[StagedFile], budget: int) -> list[StagedFile]:
    """Pack files in torrent order without exceeding the current disk budget."""
    batch = []
    used = 0
    for file in files:
        if file.size <= budget - used:
            batch.append(file)
            used += file.size
    return batch


def preflight_error(
    files: list[StagedFile], budget: int, max_upload_size: int = 0
) -> str:
    if not files:
        return "No torrent files are selected."
    largest = max(files, key=lambda item: item.size)
    if largest.size > budget:
        return f"File '{largest.name}' is larger than safe free storage."
    if max_upload_size and largest.size > max_upload_size:
        return f"File '{largest.name}' exceeds the upload destination limit."
    return ""
