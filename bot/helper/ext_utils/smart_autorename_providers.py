from contextlib import suppress
from dataclasses import dataclass
from logging import getLogger
from typing import Optional
from niquests import AsyncSession

from bot.core.config_manager import Config
from .bot_utils import sync_to_async

LOGGER = getLogger(__name__)


@dataclass
class CanonicalMetadata:
    title: Optional[str] = None
    year: Optional[str] = None
    series_title: Optional[str] = None
    episode_title: Optional[str] = None
    provider: Optional[str] = None
    provider_id: Optional[str] = None
    ott: Optional[str] = None


class IMDbProvider:
    async def find_title(
        self,
        title: str,
        year: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        def lookup():
            try:
                from imdbio import search_title, get_movie
            except ImportError:
                return None

            clean_q = title.strip().lower()
            if not clean_q:
                return None

            res = search_title(clean_q)
            results = getattr(res, "titles", []) or []
            if not results:
                return None

            if year:
                matched_year = [
                    item
                    for item in results
                    if str(getattr(item, "year", "") or "") == str(year)
                ]
                if matched_year:
                    results = matched_year

            if media_type == "movie":
                preferred = [
                    item for item in results if getattr(item, "kind", None) == "movie"
                ]
            elif media_type in ("single_episode", "episode_range", "season_pack"):
                preferred = [
                    item
                    for item in results
                    if getattr(item, "kind", None)
                    in ("tvSeries", "tvMiniSeries", "tvShow")
                ]
            else:
                preferred = [
                    item
                    for item in results
                    if getattr(item, "kind", None)
                    in ("movie", "tvSeries", "tvMiniSeries")
                ]

            target = preferred[0] if preferred else results[0]
            item_id = getattr(target, "id", None)
            if not item_id:
                return None

            m = get_movie(item_id)
            if not m:
                return None

            disp_title = getattr(m, "title", None) or getattr(target, "title", None)
            disp_year = (
                str(getattr(m, "year", None) or getattr(target, "year", None) or "")
                or None
            )

            return CanonicalMetadata(
                title=disp_title,
                year=disp_year,
                series_title=disp_title
                if media_type in ("single_episode", "episode_range", "season_pack")
                else None,
                provider="imdb",
                provider_id=f"tt{item_id}",
            )

        try:
            return await sync_to_async(lookup)
        except Exception as exc:
            LOGGER.warning(f"IMDb Smart Rename lookup failed: {exc}")
            return None

    async def find_episode(
        self,
        series_title: str,
        season: int,
        episode: int,
        year: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        def lookup():
            try:
                from imdbio import search_title, get_movie
            except ImportError:
                return None

            clean_q = series_title.strip().lower()
            if not clean_q:
                return None

            res = search_title(clean_q)
            results = getattr(res, "titles", []) or []
            if not results:
                return None

            if year:
                matched_year = [
                    item
                    for item in results
                    if str(getattr(item, "year", "") or "") == str(year)
                ]
                if matched_year:
                    results = matched_year

            series_matches = [
                item
                for item in results
                if getattr(item, "kind", None) in ("tvSeries", "tvMiniSeries", "tvShow")
            ] or results

            target = series_matches[0]
            series_id = getattr(target, "id", None)
            if not series_id:
                return None

            s_obj = get_movie(series_id)
            series_disp = (
                getattr(s_obj, "title", None)
                if s_obj
                else getattr(target, "title", None)
            )

            ep_title = None
            if s_obj and hasattr(s_obj, "episodes"):
                with suppress(Exception):
                    episodes_data = getattr(s_obj, "episodes", None)
                    if episodes_data:
                        se_eps = episodes_data.get(season, {})
                        ep_obj = se_eps.get(episode)
                        if ep_obj:
                            ep_title = getattr(ep_obj, "title", None)

            return CanonicalMetadata(
                series_title=series_disp,
                episode_title=ep_title,
                provider="imdb",
                provider_id=f"tt{series_id}",
            )

        try:
            return await sync_to_async(lookup)
        except Exception as exc:
            LOGGER.warning(f"IMDb episode lookup failed: {exc}")
            return None


TMDB_API_BASE = "https://api.themoviedb.org/3"


class TMDbProvider:
    async def _request(self, path: str, params: dict | None = None):
        token = str(Config.TMDB_ACCESS_TOKEN or "").strip()
        if not token:
            return None

        params = dict(params or {})
        headers = {"accept": "application/json"}

        if len(token) < 50:
            params["api_key"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"

        try:
            async with AsyncSession(timeout=15) as client:
                resp = await client.get(
                    f"{TMDB_API_BASE}{path}",
                    params=params,
                    headers=headers,
                )
                if resp.status_code != 200:
                    return None
                return resp.json()
        except Exception as exc:
            LOGGER.warning(f"TMDb Smart Rename request failed: {exc}")
            return None

    async def find_title(
        self,
        title: str,
        year: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        if not str(Config.TMDB_ACCESS_TOKEN or "").strip():
            return None

        endpoint = (
            "/search/movie"
            if media_type == "movie"
            else "/search/tv"
            if media_type in ("single_episode", "episode_range", "season_pack")
            else "/search/multi"
        )
        params = {"query": title, "include_adult": "false", "language": "en-US"}
        if year and media_type == "movie":
            params["year"] = str(year)
        elif year and media_type != "movie":
            params["first_air_date_year"] = str(year)

        data = await self._request(endpoint, params)
        if not data:
            return None

        results = [
            r for r in data.get("results", []) if r.get("media_type") != "person"
        ]
        if not results:
            return None

        item = results[0]
        disp_title = (
            item.get("title")
            or item.get("name")
            or item.get("original_title")
            or item.get("original_name")
        )
        rel_date = item.get("release_date") or item.get("first_air_date") or ""
        disp_year = rel_date[:4] if len(rel_date) >= 4 else None

        networks = None
        if item.get("media_type") == "tv" or media_type in (
            "single_episode",
            "episode_range",
            "season_pack",
        ):
            tv_details = await self._request(f"/tv/{item['id']}")
            if tv_details and tv_details.get("networks"):
                nets = [
                    n.get("name")
                    for n in tv_details.get("networks", [])
                    if n.get("name")
                ]
                if nets:
                    networks = nets[0]

        return CanonicalMetadata(
            title=disp_title,
            year=disp_year,
            series_title=disp_title
            if media_type in ("single_episode", "episode_range", "season_pack")
            else None,
            provider="tmdb",
            provider_id=str(item.get("id")),
            ott=networks,
        )

    async def find_episode(
        self,
        series_title: str,
        season: int,
        episode: int,
        year: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        if not str(Config.TMDB_ACCESS_TOKEN or "").strip():
            return None

        params = {"query": series_title, "include_adult": "false", "language": "en-US"}
        if year:
            params["first_air_date_year"] = str(year)

        search_data = await self._request("/search/tv", params)
        if not search_data or not search_data.get("results"):
            return None

        tv_item = search_data["results"][0]
        tv_id = tv_item["id"]
        series_disp = tv_item.get("name") or tv_item.get("original_name")

        ep_data = await self._request(f"/tv/{tv_id}/season/{season}/episode/{episode}")
        ep_title = None
        if ep_data and ep_data.get("name"):
            ep_title = ep_data.get("name")

        return CanonicalMetadata(
            series_title=series_disp,
            episode_title=ep_title,
            provider="tmdb",
            provider_id=str(tv_id),
        )


class CanonicalMetadataResolver:
    def __init__(self):
        self.imdb = IMDbProvider()
        self.tmdb = TMDbProvider()
        self.cache: dict[tuple, CanonicalMetadata] = {}

    async def resolve_title(
        self,
        title: str,
        year: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        cache_key = (
            title.strip().lower(),
            str(year or ""),
            str(media_type or ""),
            "title",
        )
        if cache_key in self.cache:
            return self.cache[cache_key]

        res = await self.imdb.find_title(title, year, media_type)
        if not res:
            res = await self.tmdb.find_title(title, year, media_type)

        if res:
            if len(self.cache) > 200:
                self.cache.clear()
            self.cache[cache_key] = res
        return res

    async def resolve_episode(
        self,
        series_title: str,
        season: int,
        episode: int,
        year: Optional[str] = None,
    ) -> Optional[CanonicalMetadata]:
        cache_key = (
            series_title.strip().lower(),
            season,
            episode,
            str(year or ""),
            "episode",
        )
        if cache_key in self.cache:
            return self.cache[cache_key]

        res = await self.imdb.find_episode(series_title, season, episode, year)
        if not res or not res.episode_title:
            tmdb_res = await self.tmdb.find_episode(series_title, season, episode, year)
            if tmdb_res:
                if res and not res.episode_title:
                    res.episode_title = tmdb_res.episode_title
                elif not res:
                    res = tmdb_res

        if res:
            if len(self.cache) > 200:
                self.cache.clear()
            self.cache[cache_key] = res
        return res
