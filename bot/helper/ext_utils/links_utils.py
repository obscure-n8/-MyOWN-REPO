from re import match as re_match
from base64 import urlsafe_b64decode, urlsafe_b64encode
from urllib.parse import urlparse


def is_magnet(url: str):
    return bool(
        re_match(
            r"^magnet:\?.*xt=urn:(btih|btmh):([a-zA-Z0-9]{32,40}|[a-z2-7]{32}).*", url
        )
    )


def is_url(url: str):
    return bool(
        re_match(
            r"^(?!\/)(rtmps?:\/\/|mms:\/\/|rtsp:\/\/|https?:\/\/|ftp:\/\/)?([^\/:]+:[^\/@]+@)?(www\.)?(?=[^\/:\s]+\.[^\/:\s]+)([^\/:\s]+\.[^\/:\s]+)(:\d+)?(\/[^#\s]*[\s\S]*)?(\?[^#\s]*)?(#.*)?$",
            url,
        )
    )


def is_gdrive_link(url: str):
    return "drive.google.com" in url or "drive.usercontent.google.com" in url


def is_telegram_link(url: str):
    return url.startswith(("https://t.me/", "tg://openmessage?user_id="))


def is_mega_link(url: str):
    return urlparse(url).netloc.lower().removeprefix("www.") in (
        "mega.nz",
        "mega.co.nz",
    )


def is_mega_folder_link(link: str) -> bool:
    if not link:
        return False
    return "/folder/" in link or "#F!" in link


def get_mega_subfolder_handle(link: str) -> str | None:
    if not link:
        return None
    parts = link.split("/folder/")
    if len(parts) >= 3:
        return parts[-1].split("#")[0].split("/")[0].split("?")[0]
    parts = link.split("#F!")
    if len(parts) >= 3:
        return parts[-1].split("!")[0].split("/")[0].split("?")[0]
    return None


def get_mega_link_type(url):
    return "folder" if "folder" in url or "/#F!" in url else "file"


def is_share_link(url: str):
    return bool(
        re_match(
            r"https?:\/\/.+\.gdtot\.\S+|https?:\/\/(filepress|filebee|appdrive|gdflix)\.\S+",
            url,
        )
    )


def is_rclone_path(path: str):
    return bool(
        re_match(
            r"^(mrcc:)?(?!(magnet:|mtp:|sa:|tp:))(?![- ])[a-zA-Z0-9_\. -]+(?<! ):(?!.*\/\/).*$|^rcl$",
            path,
        )
    )


def is_gdrive_id(id_: str):
    return bool(
        re_match(
            r"^(tp:|sa:|mtp:)?(?:[a-zA-Z0-9-_]{33}|[a-zA-Z0-9_-]{19})$|^gdl$|^(tp:|mtp:)?root$",
            id_,
        )
    )


def encode_slink(string):
    return (urlsafe_b64encode(string.encode("ascii")).decode("ascii")).strip("=")


def decode_slink(b64_str):
    return urlsafe_b64decode(
        (b64_str.strip("=") + "=" * (-len(b64_str.strip("=")) % 4)).encode("ascii")
    ).decode("ascii")


def get_magnet_from_torrent(file_path_or_bytes):
    import hashlib
    from urllib.parse import quote

    if isinstance(file_path_or_bytes, str):
        with open(file_path_or_bytes, "rb") as f:
            data = f.read()
    else:
        data = file_path_or_bytes

    def decode(data, idx=0):
        if data[idx : idx + 1] == b"i":
            idx += 1
            end = data.index(b"e", idx)
            return int(data[idx:end]), end + 1
        elif data[idx : idx + 1] == b"l":
            idx += 1
            res = []
            while data[idx : idx + 1] != b"e":
                val, idx = decode(data, idx)
                res.append(val)
            return res, idx + 1
        elif data[idx : idx + 1] == b"d":
            idx += 1
            res = {}
            while data[idx : idx + 1] != b"e":
                key, idx = decode(data, idx)
                val, idx = decode(data, idx)
                res[key] = val
            return res, idx + 1
        elif data[idx : idx + 1].isdigit():
            colon = data.index(b":", idx)
            length = int(data[idx:colon])
            start = colon + 1
            return data[start : start + length], start + length
        raise ValueError("Invalid bencode data")

    def encode(obj):
        if isinstance(obj, int):
            return b"i" + str(obj).encode() + b"e"
        elif isinstance(obj, bytes):
            return str(len(obj)).encode() + b":" + obj
        elif isinstance(obj, list):
            return b"l" + b"".join(encode(x) for x in obj) + b"e"
        elif isinstance(obj, dict):
            return (
                b"d"
                + b"".join(encode(k) + encode(obj[k]) for k in sorted(obj.keys()))
                + b"e"
            )
        raise ValueError("Cannot encode type")

    parsed, _ = decode(data)
    info_bytes = encode(parsed[b"info"])
    info_hash = hashlib.sha1(info_bytes).hexdigest()
    name = parsed.get(b"info", {}).get(b"name", b"").decode("utf-8", "ignore")
    magnet = f"magnet:?xt=urn:btih:{info_hash}"
    if name:
        magnet += f"&dn={quote(name)}"
    return magnet
