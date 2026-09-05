from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from logging import getLogger
from os import path as ospath
import re
from typing import Optional

from aiofiles.os import rename, path as aiopath

from .media_utils import get_streams, get_document_type
from .smart_autorename_providers import CanonicalMetadataResolver, CanonicalMetadata

LOGGER = getLogger(__name__)


class SmartMediaType(str, Enum):
    MOVIE = "movie"
    SINGLE_EPISODE = "single_episode"
    EPISODE_RANGE = "episode_range"
    SEASON_PACK = "season_pack"
    UNKNOWN = "unknown"


class SmartRenameError(Exception):
    pass


class SmartRenameLengthError(SmartRenameError):
    pass


class SmartRenameMetadataError(SmartRenameError):
    pass


@dataclass
class SmartFilenameContext:
    original_filename: str
    media_extension: str
    split_suffix: str = ""

    title: str = ""
    year: Optional[str] = None

    season: Optional[int] = None
    episode_start: Optional[int] = None
    episode_end: Optional[int] = None

    media_type: SmartMediaType = SmartMediaType.UNKNOWN

    ott: Optional[str] = None
    filename_quality: Optional[str] = None
    filename_codec: Optional[str] = None
    filename_audio: Optional[str] = None
    filename_esubs: bool = False


@dataclass
class SmartMediaMetadata:
    quality: Optional[str] = None
    video_codec: Optional[str] = None
    audio_language: Optional[str] = None
    subtitle_languages: tuple[str, ...] = ()
    has_english_subtitle: bool = False


@dataclass
class SmartNameParts:
    title: str
    identity: str = ""
    year: Optional[str] = None
    episode_title: Optional[str] = None
    quality: Optional[str] = None
    ott: Optional[str] = None
    audio: Optional[str] = None
    codec: Optional[str] = None
    esubs: bool = False
    extension: str = ".mkv"
    split_suffix: str = ""


_VIDEO_EXT_RE = re.compile(
    r"(?i)(?P<ext>\.(?:mkv|mp4|avi|webm|flv|mov|m4v|3gp|ts|m2ts|wmv|asf|divx|ogv|vob|mpg|mpeg))(?P<split>\.0*\d+)?$"
)

_RANGE_RE = re.compile(
    r"(?i)\bS(?P<season>\d{1,2})\s*E(?P<start>\d{1,4})\s*(?:-|–|~|\+|&|,|to|and)\s*E?(?P<end>\d{1,4})\b"
)
_SINGLE_RE = re.compile(r"(?i)\bS(?P<season>\d{1,2})\s*E(?P<episode>\d{1,4})\b")
_SEASON_RE = re.compile(r"(?i)\bS(?P<season>\d{1,2})\b")
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")

_QUALITY_RE = re.compile(
    r"(?i)(?<!\d)(480p|540p|576p|720p|1080p|1440p|2160p|4320p|4k)(?!\w)"
)
_CODEC_RE = re.compile(r"(?i)(?<!\w)(x264|x265|h264|h265|hevc|av1|vp9|vp8)(?!\w)")

OTT_ALIASES = {
    "amzn": "AMZN",
    "amazon": "AMZN",
    "nf": "NF",
    "netflix": "NF",
    "dsnp": "DSNP",
    "disney": "DSNP",
    "hmax": "HMAX",
    "max": "MAX",
    "atvp": "ATVP",
    "apple": "ATVP",
    "hulu": "Hulu",
    "hstar": "HOTSTAR",
    "hotstar": "HOTSTAR",
    "jio": "JIO",
    "sonyliv": "SONYLIV",
    "wb": "WB",
}

VIDEO_CODEC_MAP = {
    "h264": "x264",
    "avc1": "x264",
    "hevc": "x265",
    "h265": "x265",
    "hev1": "x265",
    "av1": "AV1",
    "vp9": "VP9",
    "vp8": "VP8",
    "mpeg4": "MPEG4",
    "mpeg2video": "MPEG2",
}

LANGUAGE_NAME_MAP = {
    "en": "English",
    "eng": "English",
    "english": "English",
    "hi": "Hindi",
    "hin": "Hindi",
    "hindi": "Hindi",
    "ja": "Japanese",
    "jpn": "Japanese",
    "japanese": "Japanese",
    "ko": "Korean",
    "kor": "Korean",
    "korean": "Korean",
    "ta": "Tamil",
    "tam": "Tamil",
    "tamil": "Tamil",
    "te": "Telugu",
    "tel": "Telugu",
    "telugu": "Telugu",
    "es": "Spanish",
    "spa": "Spanish",
    "spanish": "Spanish",
    "fr": "French",
    "fre": "French",
    "fra": "French",
    "french": "French",
    "de": "German",
    "ger": "German",
    "deu": "German",
    "german": "German",
    "zh": "Chinese",
    "chi": "Chinese",
    "zho": "Chinese",
    "chinese": "Chinese",
    "ru": "Russian",
    "rus": "Russian",
    "russian": "Russian",
    "it": "Italian",
    "ita": "Italian",
    "italian": "Italian",
    "ml": "Malayalam",
    "mal": "Malayalam",
    "malayalam": "Malayalam",
    "kn": "Kannada",
    "kan": "Kannada",
    "kannada": "Kannada",
    "mr": "Marathi",
    "mar": "Marathi",
    "marathi": "Marathi",
    "bn": "Bengali",
    "ben": "Bengali",
    "bengali": "Bengali",
    "pa": "Punjabi",
    "pan": "Punjabi",
    "punjabi": "Punjabi",
}


def split_media_filename(filename: str) -> tuple[str, str, str]:
    """Return (logical_stem, extension, split_suffix)."""
    match = _VIDEO_EXT_RE.search(filename)
    if match:
        logical_stem = filename[: match.start()]
        extension = match.group("ext")
        split_suffix = match.group("split") or ""
        return logical_stem, extension.lower(), split_suffix

    path_stem, path_ext = ospath.splitext(filename)
    return path_stem, path_ext.lower(), ""


def normalize_video_codec(codec_name: str | None) -> str | None:
    if not codec_name:
        return None
    value = codec_name.strip().lower()
    return VIDEO_CODEC_MAP.get(value, value.upper())


def quality_from_height(height: int | None) -> str | None:
    if not height:
        return None
    if height <= 480:
        return "480p"
    if height <= 540:
        return "540p"
    if height <= 576:
        return "576p"
    if height <= 720:
        return "720p"
    if height <= 1080:
        return "1080p"
    if height <= 1440:
        return "1440p"
    if height <= 2160:
        return "2160p"
    if height <= 4320:
        return "4320p"
    return f"{height}p"


def normalize_language(lang_code: str | None) -> str | None:
    if not lang_code:
        return None
    code = lang_code.strip().lower()
    if code in {"und", "unknown", "none", "zxx"}:
        return None
    return LANGUAGE_NAME_MAP.get(code, code.capitalize())


def clean_candidate_title(stem: str) -> str:
    value = stem
    value = re.sub(r"https?://\S+", " ", value, flags=re.I)
    value = re.sub(r"\[[^\]]*\]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)

    value = re.sub(
        r"(?i)\bS\d{1,2}\s*E\d{1,4}\s*(?:-|–|~|\+|&|,|to|and)\s*E?\d{1,4}\b",
        " ",
        value,
    )
    value = re.sub(r"(?i)\bS\d{1,2}\s*E\d{1,4}\b", " ", value)
    value = re.sub(r"(?i)\bS\d{1,2}\b", " ", value)
    value = re.sub(r"(?i)\bSeason\s*\d{1,2}\b", " ", value)

    value = _QUALITY_RE.sub(" ", value)
    value = _CODEC_RE.sub(" ", value)
    value = re.sub(
        r"(?i)\b(?:web[- ]?dl|web[- ]?rip|webrip|bluray|brrip|hdr\d*|10bit|8bit|remux|repack|esubs?|engsubs?|multi(?:audio|sub)?|combined)\b",
        " ",
        value,
    )

    for alias in OTT_ALIASES.keys():
        value = re.sub(rf"(?i)\b{alias}\b", " ", value)

    value = re.sub(r"(?i)\b(?:ds\d*k?|uhd|4k|2k|hd)\b", " ", value)
    value = re.sub(
        r"(?i)\b(?:ddp|dd\+|dd|aac|ac3|eac3|flac|dts|truehd|atmos|mp3)(?:\s*\d\.\d)?\b",
        " ",
        value,
    )
    value = re.sub(r"\b\d\.\d\b", " ", value)
    value = re.sub(
        r"(?i)\b(?:hindi|english|tamil|telugu|malayalam|kannada|marathi|bengali|punjabi|japanese|korean|spanish|french|german|chinese|italian|russian)\b",
        " ",
        value,
    )

    value = value.replace("_", " ")
    value = re.sub(r"\.+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .-_-")


def parse_smart_filename(filename: str) -> SmartFilenameContext:
    logical_stem, extension, split_suffix = split_media_filename(filename)

    range_match = _RANGE_RE.search(logical_stem)
    single_match = _SINGLE_RE.search(logical_stem)
    season_match = _SEASON_RE.search(logical_stem)

    season = None
    episode_start = None
    episode_end = None

    if range_match:
        media_type = SmartMediaType.EPISODE_RANGE
        season = int(range_match.group("season"))
        episode_start = int(range_match.group("start"))
        episode_end = int(range_match.group("end"))
        marker = range_match
    elif single_match:
        media_type = SmartMediaType.SINGLE_EPISODE
        season = int(single_match.group("season"))
        episode_start = int(single_match.group("episode"))
        marker = single_match
    elif season_match:
        media_type = SmartMediaType.SEASON_PACK
        season = int(season_match.group("season"))
        marker = season_match
    else:
        media_type = SmartMediaType.MOVIE
        marker = None

    title_source = logical_stem
    if marker:
        pre_marker = logical_stem[: marker.start()].strip(" .-_-")
        if pre_marker:
            title_source = pre_marker

    year_match = _YEAR_RE.search(title_source)
    year = year_match.group(0) if year_match else None
    if year_match:
        title_source = (
            title_source[: year_match.start()] + " " + title_source[year_match.end() :]
        )

    title_clean = clean_candidate_title(title_source)

    quality = None
    if q := _QUALITY_RE.search(logical_stem):
        q_str = q.group(1).lower()
        quality = "2160p" if q_str == "4k" else q_str

    codec = None
    if c := _CODEC_RE.search(logical_stem):
        codec = normalize_video_codec(c.group(1))

    esubs = bool(re.search(r"(?i)\b(?:esubs?|eng(?:lish)?subs?)\b", logical_stem))

    ott = None
    for alias, canonical in OTT_ALIASES.items():
        if re.search(rf"(?i)\b{alias}\b", logical_stem):
            ott = canonical
            break

    audio = None
    for code, name in LANGUAGE_NAME_MAP.items():
        if len(code) > 2 and re.search(rf"(?i)\b{code}\b", logical_stem):
            audio = name
            break

    return SmartFilenameContext(
        original_filename=filename,
        media_extension=extension,
        split_suffix=split_suffix,
        title=title_clean,
        year=year,
        season=season,
        episode_start=episode_start,
        episode_end=episode_end,
        media_type=media_type,
        ott=ott,
        filename_quality=quality,
        filename_codec=codec,
        filename_audio=audio,
        filename_esubs=esubs,
    )


async def probe_smart_media_metadata(
    file_path: str, ctx: SmartFilenameContext
) -> SmartMediaMetadata:
    streams = await get_streams(file_path) or []

    video = next(
        (s for s in streams if s.get("codec_type") == "video"),
        None,
    )

    audio_language = None
    subtitle_languages = []

    for stream in streams:
        codec_type = stream.get("codec_type")
        language = ((stream.get("tags") or {}).get("language") or "").strip()

        if codec_type == "audio" and not audio_language and language:
            if language.lower() not in {"und", "unknown", "none"}:
                audio_language = normalize_language(language)

        elif codec_type == "subtitle" and language:
            normalized = normalize_language(language)
            if normalized:
                subtitle_languages.append(normalized)

    height = None
    codec = None
    if video:
        with suppress(Exception):
            height = int(video.get("height"))
        codec = normalize_video_codec(video.get("codec_name"))

    english = any(lang.lower() in {"english", "en"} for lang in subtitle_languages)

    return SmartMediaMetadata(
        quality=quality_from_height(height) or ctx.filename_quality,
        video_codec=codec or ctx.filename_codec,
        audio_language=audio_language or ctx.filename_audio,
        subtitle_languages=tuple(subtitle_languages),
        has_english_subtitle=english or ctx.filename_esubs,
    )


def clean_component(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*]", " ", str(value))
    value = re.sub(r"[-–—]", " ", value)
    value = re.sub(r"[.]+", " ", value)
    value = re.sub(r"\s+", ".", value).strip(".")
    return value


def shorten_words(title: str, max_chars: int) -> str | None:
    title = clean_component(title)
    if len(title) <= max_chars:
        return title
    if max_chars <= 0:
        return None

    words = title.split()
    result = ""
    for word in words:
        candidate = word if not result else f"{result}.{word}"
        if len(candidate) <= max_chars:
            result = candidate
        else:
            break

    if result:
        return result

    return title[:max_chars].rstrip(".- ") or None


class SmartFilenameBuilder:
    def make_parts(
        self,
        ctx: SmartFilenameContext,
        canonical: Optional[CanonicalMetadata],
        media: SmartMediaMetadata,
    ) -> Optional[SmartNameParts]:
        if ctx.media_type == SmartMediaType.MOVIE:
            return self._make_movie_parts(ctx, canonical, media)

        if ctx.media_type == SmartMediaType.SINGLE_EPISODE:
            return self._make_single_episode_parts(ctx, canonical, media)

        if ctx.media_type == SmartMediaType.EPISODE_RANGE:
            return self._make_episode_range_parts(ctx, canonical, media)

        if ctx.media_type == SmartMediaType.SEASON_PACK:
            return self._make_season_pack_parts(ctx, canonical, media)

        return None

    def _technical_fields(self, ctx, media, canonical):
        quality = media.quality or ctx.filename_quality
        ott = (canonical.ott if canonical else None) or ctx.ott
        audio = media.audio_language or ctx.filename_audio
        codec = media.video_codec or ctx.filename_codec
        esubs = media.has_english_subtitle or ctx.filename_esubs
        return quality, ott, audio, codec, esubs

    def _make_movie_parts(self, ctx, canonical, media):
        title = (canonical.title if canonical else None) or ctx.title
        year = (canonical.year if canonical else None) or ctx.year

        if not title or not year:
            return None

        quality, ott, audio, codec, esubs = self._technical_fields(
            ctx, media, canonical
        )

        return SmartNameParts(
            title=title,
            identity="",
            year=year,
            quality=quality,
            ott=ott,
            audio=audio,
            codec=codec,
            esubs=esubs,
            extension=ctx.media_extension,
            split_suffix=ctx.split_suffix,
        )

    def _make_single_episode_parts(self, ctx, canonical, media):
        series_title = (
            canonical.series_title or canonical.title if canonical else None
        ) or ctx.title
        episode_title = canonical.episode_title if canonical else None

        if not series_title or ctx.season is None or ctx.episode_start is None:
            return None

        se = f"S{ctx.season:02d}E{ctx.episode_start:02d}"
        quality, ott, audio, codec, esubs = self._technical_fields(
            ctx, media, canonical
        )

        return SmartNameParts(
            title=series_title,
            identity=se,
            episode_title=episode_title,
            quality=quality,
            ott=ott,
            audio=audio,
            codec=codec,
            esubs=esubs,
            extension=ctx.media_extension,
            split_suffix=ctx.split_suffix,
        )

    def _make_episode_range_parts(self, ctx, canonical, media):
        series_title = (
            canonical.series_title or canonical.title if canonical else None
        ) or ctx.title

        if (
            not series_title
            or ctx.season is None
            or ctx.episode_start is None
            or ctx.episode_end is None
        ):
            return None

        season_range = (
            f"S{ctx.season:02d}E{ctx.episode_start:02d}-E{ctx.episode_end:02d}"
        )
        quality, ott, audio, codec, esubs = self._technical_fields(
            ctx, media, canonical
        )

        return SmartNameParts(
            title=series_title,
            identity=season_range,
            quality=quality,
            ott=ott,
            audio=audio,
            codec=codec,
            esubs=esubs,
            extension=ctx.media_extension,
            split_suffix=ctx.split_suffix,
        )

    def _make_season_pack_parts(self, ctx, canonical, media):
        series_title = (
            canonical.series_title or canonical.title if canonical else None
        ) or ctx.title

        if not series_title or ctx.season is None:
            return None

        season_str = f"S{ctx.season:02d}"
        quality, ott, audio, codec, esubs = self._technical_fields(
            ctx, media, canonical
        )

        return SmartNameParts(
            title=series_title,
            identity=season_str,
            quality=quality,
            ott=ott,
            audio=audio,
            codec=codec,
            esubs=esubs,
            extension=ctx.media_extension,
            split_suffix=ctx.split_suffix,
        )


TV_REDUCTION_ORDER = (
    "episode_title",
    "esubs",
    "ott",
    "audio",
    "codec",
)

MOVIE_REDUCTION_ORDER = (
    "esubs",
    "ott",
    "audio",
    "codec",
)


class SmartNameFitter:
    def fit(
        self,
        parts: SmartNameParts,
        media_type: SmartMediaType,
        prefix: str = "",
        suffix: str = "",
    ) -> Optional[str]:
        clean_prefix = re.sub(r"<.*?>", "", prefix or "").replace(r"\s", " ")
        clean_suffix = re.sub(r"<.*?>", "", suffix or "").replace(r"\s", " ")

        affix_overhead = len(clean_prefix) + len(clean_suffix)
        max_target_length = 60 - affix_overhead
        if max_target_length <= 0:
            return None

        def render():
            title_c = clean_component(parts.title)
            values = [title_c]
            if parts.identity:
                values.append(clean_component(parts.identity))
            if parts.year:
                values.append(clean_component(parts.year))
            if parts.episode_title:
                values.append(clean_component(parts.episode_title))
            if parts.quality:
                values.append(clean_component(parts.quality))
            if parts.ott:
                values.append(clean_component(parts.ott))
            if parts.audio:
                values.append(clean_component(parts.audio))
            if parts.codec:
                values.append(clean_component(parts.codec))
            if parts.esubs:
                values.append("ESubs")

            base = ".".join(v for v in values if v)
            return f"{clean_prefix}{base}{clean_suffix}{parts.extension}{parts.split_suffix}"

        candidate = render()
        if len(candidate) <= 60:
            return candidate

        reduction_order = (
            TV_REDUCTION_ORDER
            if media_type != SmartMediaType.MOVIE
            else MOVIE_REDUCTION_ORDER
        )

        for field in reduction_order:
            if field == "esubs":
                parts.esubs = False
            else:
                setattr(parts, field, None)

            candidate = render()
            if len(candidate) <= 60:
                return candidate

        fixed_parts = []
        if parts.identity:
            fixed_parts.append(clean_component(parts.identity))
        if parts.year:
            fixed_parts.append(clean_component(parts.year))
        if parts.quality:
            fixed_parts.append(clean_component(parts.quality))
        fixed_str = ".".join(fixed_parts)
        if fixed_str:
            fixed_str = f".{fixed_str}"

        non_title_overhead = len(
            f"{clean_prefix}{fixed_str}{clean_suffix}{parts.extension}{parts.split_suffix}"
        )
        title_budget = 60 - non_title_overhead

        shortened = shorten_words(parts.title, title_budget)
        if shortened:
            parts.title = shortened
            candidate = render()
            if len(candidate) <= 60:
                return candidate

        return None


class SmartAutoRename:
    def __init__(self, resolver: Optional[CanonicalMetadataResolver] = None):
        self.resolver = resolver or CanonicalMetadataResolver()
        self.builder = SmartFilenameBuilder()
        self.fitter = SmartNameFitter()

    async def rename(
        self,
        file_path: str,
        prefix: str = "",
        suffix: str = "",
    ) -> str:
        filename = ospath.basename(file_path)

        if not await is_video_media(file_path):
            LOGGER.info(f"Smart Autorename skipped (non-video media): {filename}")
            return file_path

        ctx = parse_smart_filename(filename)
        if ctx.media_type == SmartMediaType.UNKNOWN:
            LOGGER.info(f"Smart Autorename skipped (unknown media type): {filename}")
            return file_path

        media = await probe_smart_media_metadata(file_path, ctx)

        canonical = None
        if (
            ctx.media_type == SmartMediaType.SINGLE_EPISODE
            and ctx.season is not None
            and ctx.episode_start is not None
        ):
            canonical = await self.resolver.resolve_episode(
                series_title=ctx.title,
                season=ctx.season,
                episode=ctx.episode_start,
                year=ctx.year,
            )
        elif ctx.title:
            canonical = await self.resolver.resolve_title(
                title=ctx.title,
                year=ctx.year,
                media_type=ctx.media_type.value,
            )

        parts = self.builder.make_parts(ctx, canonical, media)
        if not parts:
            LOGGER.warning(
                f"Smart Autorename skipped: required metadata unavailable for {filename}"
            )
            return file_path

        new_name = self.fitter.fit(
            parts,
            media_type=ctx.media_type,
            prefix=prefix,
            suffix=suffix,
        )

        if not new_name:
            raise SmartRenameLengthError(
                "Cannot perform Smart Autorename: the required filename exceeds Telegram's 60-character limit."
            )

        new_path = ospath.join(ospath.dirname(file_path), new_name)
        if new_path != file_path:
            if await aiopath.exists(new_path):
                if file_path != new_path:
                    raise SmartRenameError(f"Target already exists: {new_path}")
                return new_path

            LOGGER.info(f"Smart Autorename: {filename} -> {new_name}")
            await rename(file_path, new_path)

        return new_path


async def is_video_media(file_path: str) -> bool:
    doc_type = await get_document_type(file_path)
    if doc_type and isinstance(doc_type, (tuple, list)) and doc_type[0]:
        return True
    stem, ext, split = split_media_filename(ospath.basename(file_path))
    return ext in {
        ".mkv",
        ".mp4",
        ".avi",
        ".webm",
        ".flv",
        ".mov",
        ".m4v",
        ".3gp",
        ".ts",
        ".m2ts",
        ".wmv",
        ".asf",
        ".divx",
        ".ogv",
        ".vob",
        ".mpg",
        ".mpeg",
    }
