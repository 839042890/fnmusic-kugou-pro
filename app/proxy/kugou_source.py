# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 ai2ku (https://github.com/ai2ku/)
# Copyright (C) 2026 星痕在此 (fnmusic_kugou_pro fork, 独立维护)
#
# 本程序是自由软件：你可以按照 GNU 通用公共许可证（第 2 版或更高版本）
# 的条件重新发布和/或修改它。完整许可证见仓库根目录 LICENSE 文件。

"""KuGouMusicApi 源适配模块。

把 KuGouMusicApi 的核心接口封装为 fnos_music_ext 期望的契约：
- search(keyword, limit) → list[dict]
- resolve_url(song_id) → str | None
- get_info(guid) → dict
- fetch_lyric(song_id) → str
- qr_login() → QR 登录相关

所有配置项通过 CONF 字典注入，由 app.py 在启动时填充。
"""
from __future__ import annotations

import asyncio
import logging
import json
import re
import time
from typing import Any

import httpx

logger = logging.getLogger("kugou_source")

# 默认值，app.py 启动时通过 set_config() 覆盖
CFG: dict[str, Any] = {
    "kugou_url": "http://127.0.0.1:8899",
    "kugou_quality": "320",
    "kugou_search_timeout": 15.0,
    "kugou_enabled": True,
    # QR 登录凭证
    "kugou_token": "",
    "kugou_userid": "",
    "kugou_dfid": "",
    "kugou_t1": "",
    "kugou_mid": "",
    "kugou_guid": "",
    "kugou_dev": "",
    "kugou_mac": "",
}


def set_config(cfg: dict[str, Any]) -> None:
    """供 app.py 调用，注入运行时配置。"""
    CFG.update(cfg)


def _auth_header() -> str | None:
    """构造 Authorization 头。"""
    parts = []
    if CFG.get("kugou_token"):
        parts.append(f"token={CFG['kugou_token']}")
    if CFG.get("kugou_userid"):
        parts.append(f"userid={CFG['kugou_userid']}")
    if CFG.get("kugou_dfid"):
        parts.append(f"dfid={CFG['kugou_dfid']}")
    if CFG.get("kugou_t1"):
        parts.append(f"t1={CFG['kugou_t1']}")
    if CFG.get("kugou_mid"):
        parts.append(f"KUGOU_API_MID={CFG['kugou_mid']}")
    if CFG.get("kugou_guid"):
        parts.append(f"KUGOU_API_GUID={CFG['kugou_guid']}")
    if CFG.get("kugou_dev"):
        parts.append(f"KUGOU_API_DEV={CFG['kugou_dev']}")
    if CFG.get("kugou_mac"):
        parts.append(f"KUGOU_API_MAC={CFG['kugou_mac']}")
    if not parts:
        return None
    return ";".join(parts)


def _client() -> httpx.AsyncClient:
    """每次调用新建客户端，避免连接池状态问题。"""
    headers = {}
    auth = _auth_header()
    if auth:
        headers["Authorization"] = auth
    return httpx.AsyncClient(
        base_url=CFG["kugou_url"],
        timeout=float(CFG["kugou_search_timeout"]),
        follow_redirects=True,
        headers=headers,
    )


# ============================================================
# 回包信封判定（status 与 error_code 必须分开读）
# ============================================================

# 20010 = 缺参数（探活请求总是带齐 token/userid，出现它就说明凭证本身不可用）
# 20017 = 凭证不可用：token 假/短/非 hex、缺 userid、userid 不存在，全回这个码
# 20018 = /user/detail 上观察到的失效码
# 实测（2026-11-04，hermes）：正常 = status:1 error_code:0；失败 = status:0 + error_code:<码>
_INVALID_ERROR_CODES = (20010, 20017, 20018)


def envelope_state(body: Any) -> str:
    """判断 KuGouMusicApi 的统一回包信封：valid / invalid / unknown。

    **必须分开读 status 与 error_code。** 历史写法
    ``body.get("status", body.get("error_code"))`` 里 ``status`` 键恒存在
    （失败时是 0），``error_code`` 那半边永远读不到，于是「status=0 + error_code=20017」
    会被判成成功。判定顺序必须是**先 error_code 后 status**：``status=0`` 是失败态，
    只有 ``error_code==0 且 status==1`` 才算成功。

    unknown 的含义是「没结论」：既不能当成功，也不能当凭证失效（上游抖动时误报失效
    会把用户推去白扫一次码）。
    """
    if not isinstance(body, dict) or not body:
        # 空回包单独判：更可能是网关错误页/空响应，当"成功"会把上游坏了看成调用成功
        return "unknown"
    ec = body.get("error_code")
    st = body.get("status")
    if ec in _INVALID_ERROR_CODES:
        return "invalid"
    if ec == 0 and st == 1:
        return "valid"
    # 兼容旧口径：信封本身正常（success 码是 200），不算失败。
    # 注意 **不含 st=None**（键缺失）与 status=0 —— 它们落到下面的 unknown。
    if ec in (0, None) and st in (1, 200):
        return "valid"
    return "unknown"


def envelope_detail(body: Any) -> str:
    """回包信封的日志摘要（只有码值，不含任何凭证）。"""
    if not isinstance(body, dict):
        return f"body_type={type(body).__name__}"
    return f"status={body.get('status')} error_code={body.get('error_code')}"


# ============================================================
# 内存索引：搜索时缓存，get_info 命中时直接返回元数据
# ============================================================

_search_index: dict[str, dict] = {}

# /album/detail?id=<id> 聚合结果缓存：detail 接口单张很快，但同一专辑详情页
# 会被封面/曲目/详情多次触发，缓存避免反复打两次酷狗接口。
_ALBUM_DETAIL_CACHE: dict[str, tuple[float, dict]] = {}
_ALBUM_DETAIL_TTL_S = 6 * 60 * 60


def _remember_song(item: dict) -> None:
    sid = str(item.get("id") or "").split(":", 1)[-1]
    if sid:
        _search_index[sid] = dict(item)


def get_search_index(song_id: str) -> dict | None:
    """读内存索引里的单曲元数据（album/artist 等）；未命中返回 None。

    本地曲目走封面回写时，若上游 track.album 为空则用此函数
    拿酷狗侧已搜过的专辑名强制覆盖。未命中时调用方应走
    /search 回源匹配。
    """
    if not song_id:
        return None
    sid = str(song_id).split(":", 1)[-1].strip()
    cached = _search_index.get(sid)
    return dict(cached) if cached else None




# ============================================================
# 搜索
# ============================================================

def _singers_from_any(it: dict) -> list[dict]:
    """统一歌手数组提取：优先 singerinfo，搜索格式优先 data.lists[].Singers。"""
    raw_singers = it.get("Singers") or it.get("singers") or it.get("singerinfo")
    if isinstance(raw_singers, str):
        name = raw_singers.strip()
        return [{"name": name, "id": ""}] if name else []
    if isinstance(it.get("singerinfo"), list):
        artists = []
        for singer in it.get("singerinfo"):
            if isinstance(singer, dict):
                name = str(singer.get("name") or "").strip()
                if name:
                    artists.append(singer)
        return artists
    if isinstance(raw_singers, list):
        artists = []
        for singer in raw_singers:
            if isinstance(singer, dict):
                name = str(singer.get("Name") or singer.get("name") or "").strip()
                singer_id = str(singer.get("ID") or singer.get("id") or singer.get("SingerID") or singer.get("singerId") or "").strip()
                if name:
                    artists.append({"name": name, "id": singer_id})
            else:
                name = str(singer or "").strip()
                if name:
                    artists.append({"name": name, "id": ""})
        return artists
    for key in ("author_name", "authorName", "AuthorName", "singername", "singerName", "SingerName", "singer", "SingName", "singName", "artist"):
        name = str(it.get(key) or "").strip()
        if name:
            raw_id = it.get("SingerID") or it.get("singerId") or it.get("authorID") or it.get("authorId")
            # key 不存在时 .get() 返回 None，直接 str() 会得到字符串 "None"，
            # 下游会拼出 online:kugou:artist:None 的脏 guid
            singer_id = str(raw_id).strip() if raw_id not in (None, "") else ""
            # /artist/audios 的 author_name 常是多人："安苏羽、橙大蕾蕾" / "IN-K、安苏羽、傅梦彤"
            parts = [p.strip() for p in re.split(r"[、,，/]", name) if p.strip()]
            return [{"name": p, "id": singer_id} for p in parts]
    return []








def _format_from_any(it: dict) -> str:
    """统一格式：优先 extname，兼容旧 ExtName/songType/format。"""
    for key in ("extname", "extName", "ExtName", "songType", "format", "ext"):
        value = it.get(key)
        if value not in (None, ""):
            return str(value).strip().lower() or "mp3"
    return "mp3"


def _cover_from_any(it: dict) -> str:
    """统一封面：保留 {size} 占位符，供封面接口动态替换。"""
    for key in ("cover", "Image", "image", "Img", "img", "AlbumImg", "albumImg", "AlbumImage", "albumImage", "PicUrl", "picUrl", "coverImg", "coverImgUrl", "AlbumImage", "albumImage", "albumImg", "albumimg", "album_img"):
        value = it.get(key)
        if value not in (None, ""):
            return str(value).strip()
    trans_param = it.get("trans_param")
    if isinstance(trans_param, dict):
        for key in ("union_cover", "UnionCover", "cover", "image"):
            value = trans_param.get(key)
            if value not in (None, ""):
                return str(value).strip()
    return ""


def _pick_ci(it: dict, *keys: str, default: str = "") -> str:
    """大小写/命名不敏感取字段：先看精确 key，再按小写归一化匹配。

    KuGouMusicApi 同一接口的回参在不同鉴权状态下字段风格不同
    （filehash / fileHash / FileHash；songname / songName / OriSongName 等），
    精确匹配会让整首歌被丢弃，所以映射统一走这里。
    """
    if not isinstance(it, dict):
        return default
    for key in keys:
        value = it.get(key)
        if value not in (None, ""):
            return str(value).strip()
    lowered = {str(k).lower(): v for k, v in it.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return str(value).strip()
    return default




def search_item_to_raw(it: dict) -> dict:
    """把酷狗搜索/歌单歌曲对象都转成统一内部格式。"""
    if not isinstance(it, dict):
        return {}
    # 歌单 track/all 的真实字段完整，直接走精确路径。
    if isinstance(it.get("singerinfo"), list) or isinstance(it.get("albuminfo"), dict):
        return track_item_to_raw(it)

    # /artist/audios 的真实回参（必须先于 /privilege/lite 判断！两者都有 hash 键，
    # 先判 lite 会读不到字段导致标题/歌手/时长全空）。
    # 真实字段：hash / audio_name / author_name(可多人，顿号分隔) / album_id / album_name /
    # timelength(毫秒) / filesize / bitrate / extname / publish_date / songid / audio_id。
    # 封面不在顶层，在 trans_param.union_cover（带 {size} 占位符）。
    # 兼容另一种风格：filehash / songname / playTime(毫秒)。
    if "audio_name" in it or (it.get("author_name") not in (None, "") and "timelength" in it):
        sid = _pick_ci(it, "hash", "filehash", "hash_128", "FileHash")
        if not sid:
            return {}
        singers = _singers_from_any(it)
        # author_name 可能是多人："安苏羽、橙大蕾蕾" -> 拆成多个 artist
        if len(singers) <= 1:
            author_name = _pick_ci(it, "author_name", "authorName", "singername")
            if author_name and (not singers or not singers[0].get("name")):
                first_id = str(it.get("authorID") or it.get("authorId") or it.get("SingerID") or "").strip()
                singers = [
                    {"name": part.strip(), "id": first_id}
                    for part in re.split(r"[、,，/]", author_name)
                    if part.strip()
                ]
        singer_name = "、".join(
            str(x.get("name") or "").strip() for x in singers if isinstance(x, dict) and str(x.get("name") or "").strip()
        )
        album_name = _pick_ci(it, "album_name", "albumName", "albumname")
        raw_name = _pick_ci(it, "audio_name", "songname", "name", "OriSongName")
        # 注意：_strip_singer_prefix_from_name 期望 list[str]，不是 dict 列表
        title = _strip_singer_prefix_from_name(raw_name, [singer_name] if singer_name else [], album_name)
        pref = str(CFG.get("kugou_quality") or "high").strip().lower()
        quality_key = {
            "high": "high",
            "flac": "flac",
            "320": "320",
            "128": "128",
        }.get(pref, "high")
        # timelength / playTime 都是毫秒；按配置音质读对应 *_flac / *_high / *_320 / *_128。
        duration_ms = _to_int(
            it.get(f"timelength_{quality_key}")
            or it.get("timelength")
            or it.get("playTime")
            or it.get("timelength_128")
            or 0
        )
        if not duration_ms:
            duration_ms = _to_int(it.get("duration") or 0)
        duration = _to_float(duration_ms)
        ext = "flac" if quality_key in {"flac", "high"} else "mp3"
        file_size = _to_int(
            it.get(f"filesize_{quality_key}")
            or it.get("filesize_128")
            or it.get("size")
            or 0
        )
        bitrate = _to_int(it.get(f"bitrate_{quality_key}") or it.get("bitrate_128") or it.get("bitrate") or 0)
        cover = _cover_from_any(it)
        return {
            "id": f"kugou:{sid}",
            "source": "kugou",
            "hash": sid,
            "title": title,
            "artist": singer_name,
            "artists": singers,
            "album": album_name,
            "album_id": _pick_ci(it, "album_id", "albumId"),
            "duration_s": duration,
            "ext": ext,
            "cover_url": cover,
            "union_cover": cover,
            "file_size": file_size,
            "bitrate": bitrate,
            "lyric": "",
            "release_date": _pick_ci(it, "publish_date", "publishDate", "release_date"),
        }

    # /privilege/lite 的真实回参按小写字段精确映射。
    if "hash" in it or "singername" in it or "albumname" in it:
        sid = str(it.get("hash") or "").strip()
        if not sid:
            return {}
        raw_name = str(it.get("name") or "").strip()
        singername = str(it.get("singername") or "").strip()
        albumname = str(it.get("albumname") or "").strip()
        title = _strip_singer_prefix_from_name(raw_name, [singername] if singername else [], albumname)
        return {
            "id": f"kugou:{sid}",
            "source": "kugou",
            "hash": sid,
            "title": title,
            "artist": singername,
            "artists": [{"name": singername, "id": ""}] if singername else [],
            "album": albumname,
            "album_id": str(it.get("album_id") or "").strip(),
            "duration_s": _to_int(it.get("duration") or 0),
            "ext": str(it.get("extname") or it.get("quality") or "mp3").strip().lower() or "mp3",
            "cover_url": str(it.get("cover") or it.get("image") or "").strip(),
            "union_cover": str(it.get("cover") or it.get("image") or "").strip(),
            "file_size": _to_int(it.get("filesize") or it.get("file_size") or it.get("size") or 0),
            "bitrate": _to_int(it.get("bitrate") or 0),
            "lyric": "",
        }

    sid = str(it.get("FileHash") or "").strip()
    if not sid:
        return {}
    artists = [
        {
            "name": str(singer.get("name") or "").strip(),
            "id": str(singer.get("id") or "").strip(),
        }
        for singer in (it.get("Singers") or [])
        if isinstance(singer, dict) and str(singer.get("name") or "").strip()
    ]
    album_id = str(it.get("AlbumID") or "").strip()
    album_name = str(it.get("AlbumName") or "").strip()
    artist = "、".join(str(x.get("name") or "").strip() for x in artists if str(x.get("name") or "").strip())
    title = str(it.get("OriSongName") or "").strip()
    suffix = str(it.get("Suffix") or "").strip()
    if suffix:
        title = f"{title} {suffix}" if title else suffix
    pref = str(CFG.get("kugou_quality") or "high").strip().lower()
    block_order = {
        "high": ("Res", "SQ", "HQ", None),
        "flac": ("SQ", "Res", "HQ", None),
        "320": ("HQ", "Res", "SQ", None),
        "128": (None, "HQ", "SQ", "Res"),
    }.get(pref, ("Res", "SQ", "HQ", None))
    selected = {}
    selected_name = ""
    for name in block_order:
        if name is None:
            break
        candidate = it.get(name)
        if isinstance(candidate, dict) and (
            candidate.get("FileHash") or candidate.get("Hash") or candidate.get("BitRate") or candidate.get("Bitrate")
        ):
            selected = candidate
            selected_name = name
            break
    selected_upper = selected_name.upper()
    ext = "flac" if selected_upper in {"SQ", "RES"} else "mp3"
    cover = str(selected.get("Image") or it.get("Image") or "").strip()
    file_size = _to_int(selected.get("FileSize") or it.get("FileSize") or 0)
    bitrate = _to_int(selected.get("BitRate") or selected.get("Bitrate") or it.get("Bitrate") or 0)
    duration = _to_int(selected.get("TimeLength") or selected.get("Duration") or it.get("Duration") or 0) * 1000
    return {
        "id": f"kugou:{sid}",
        "source": "kugou",
        "hash": sid,
        "title": title,
        "artist": artist,
        "artists": artists,
        "album": album_name,
        "album_id": album_id,
        "duration_s": duration,
        "ext": ext,
        "cover_url": cover,
        "union_cover": cover,
        "file_size": file_size,
        "bitrate": bitrate,
        "lyric": "",
        **({"_matched_tier": {"quality": pref, "block": selected_name}} if selected_name else {}),
    }


async def search(keyword: str, limit: int = 30, page: int = 1) -> dict:
    """调 KuGouMusicApi /search，返回统一结果对象，包含 items 和 total。"""
    if not keyword:
        return {"items": [], "total": 0, "page": page, "pagesize": limit}
    auth = _auth_header()
    logger.warning("[KUGOU] keyword=%r limit=%d auth_len=%d",
                   keyword, limit, len(auth or ""))
    try:
        async with _client() as c:
            lists: list[dict] = []
            raw: dict = {}
            last_body = ""
            for attempt in range(5):  # 5 次重试，应对 data:null
                r = await c.get(
                    "/search",
                    params={"keywords": keyword, "page": page, "pagesize": limit, "type": "song"},
                )
                last_body = r.text[:200]
                if r.status_code != 200:
                    logger.warning("[KUGOU] http %s attempt=%d", r.status_code, attempt + 1)
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                body = r.json()
                if body.get("status") != 1:
                    logger.warning("[KUGOU] status=%s attempt=%d", body.get("status"), attempt + 1)
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                d = body.get("data") or {}
                lists = d.get("lists") or []
                raw = d
                if lists:
                    break
                logger.warning("[KUGOU] empty lists attempt=%d body=%r", attempt + 1, last_body[:120])
                await asyncio.sleep(0.6 * (attempt + 1))
            logger.warning("[KUGOU] final lists=%d", len(lists))
    except Exception as e:
        logger.warning("[KUGOU] error: %s", e)
        return {"items": [], "total": 0, "page": page, "pagesize": limit}

    items: list[dict] = []
    for it in lists:
        if not isinstance(it, dict):
            continue
        raw_item = search_item_to_raw(it)
        if not raw_item:
            continue
        items.append(raw_item)
        _remember_song(raw_item)

    try:
        kugou_total = int(raw.get("total") or 0)
    except (TypeError, ValueError):
        kugou_total = 0

    return {
        "items": items,
        "total": max(kugou_total, len(items)),
        "page": page,
        "pagesize": limit,
    }


async def get_artist_audios(artist_id: int | str, sort: str = "hot", page: int = 1, pagesize: int = 60) -> dict:
    """调 KuGouMusicApi /artist/audios，返回统一结果对象。

    接口：/artist/audios?id=3520&sort=hot&page=1&pagesize=60
    回参：{"total": n, "error_code": 0, "data": [...]}。
    """
    aid = str(artist_id or "").strip()
    if not aid:
        return {"items": [], "total": 0, "page": page, "pagesize": pagesize}
    try:
        async with _client() as c:
            r = await c.get(
                "/artist/audios",
                params={"id": aid, "sort": sort or "hot", "page": page, "pagesize": pagesize},
            )
            if r.status_code != 200:
                logger.warning("[KUGOU] artist/audios http=%s aid=%s body=%r", r.status_code, aid, r.text[:200])
                return {"items": [], "total": 0, "page": page, "pagesize": pagesize}
            body = r.json()
            if not isinstance(body, dict):
                logger.warning("[KUGOU] artist/audios body type=%s aid=%s body=%r", type(body).__name__, aid, str(body)[:200])
                return {"items": [], "total": 0, "page": page, "pagesize": pagesize}
            data = body.get("data") or []
            if isinstance(data, dict):
                data = data.get("list") or data.get("items") or []
            raw_items = data if isinstance(data, list) else []
            items: list[dict] = []
            for it in raw_items:
                if not isinstance(it, dict):
                    continue
                raw_item = search_item_to_raw(it)
                if not raw_item:
                    continue
                raw_item["release_date"] = str(it.get("publish_date") or it.get("publishDate") or "").strip()
                items.append(raw_item)
                _remember_song(raw_item)
            try:
                total = int(body.get("total") or 0)
            except (TypeError, ValueError):
                total = len(items)
            return {
                "items": items,
                "total": max(total, len(items)),
                "page": page,
                "pagesize": pagesize,
                "artist_id": aid,
            }
    except Exception as e:
        logger.warning("[KUGOU] artist/audios error aid=%s: %s", aid, e)
        return {"items": [], "total": 0, "page": page, "pagesize": pagesize}


async def search_albums_raw(keyword: str, limit: int = 30, page: int = 1) -> dict:
    """调 KuGouMusicApi /search?type=album，**原始透传** lists。

    与 search() 不同：不做 search_item_to_raw 映射（那是歌曲口径），也不
    写 _search_index——/album/detail 聚合要的是专辑原字段。

    回参 data.lists[] 真实字段：albumid / albumname / singers[{name,id}] /
    singerids[] / img(完整 240 URL) / songcount / publish_time / language /
    intro / company / title / quality / category / special_tag
    """
    kw = str(keyword or "").strip()
    if not kw:
        return {"lists": [], "total": 0, "page": page, "pagesize": limit, "keyword": ""}
    try:
        async with _client() as c:
            r = await c.get(
                "/search",
                params={"keywords": kw, "page": page, "pagesize": limit, "type": "album"},
            )
            if r.status_code != 200:
                logger.warning("[KUGOU] search/type=album http=%s kw=%r body=%r",
                               r.status_code, kw, r.text[:200])
                return {"lists": [], "total": 0, "page": page, "pagesize": limit, "keyword": kw}
            body = r.json()
            # 先 error_code 后 status（status=0 是失败态），见 envelope_state 注释
            if envelope_state(body) != "valid":
                logger.warning("[KUGOU] search/type=album %s kw=%r errmsg=%r",
                               envelope_detail(body), kw, body.get("error_msg"))
                return {"lists": [], "total": 0, "page": page, "pagesize": limit, "keyword": kw}
            data = body.get("data") or {}
            lists = data.get("lists") or [] if isinstance(data, dict) else []
            lists = [x for x in lists if isinstance(x, dict)] if isinstance(lists, list) else []
            try:
                total = int(data.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            logger.warning("[KUGOU] search/type=album kw=%r got=%d total=%d",
                           kw, len(lists), total)
            return {
                "lists": lists,
                "total": max(total, len(lists)),
                "page": page,
                "pagesize": limit,
                "keyword": kw,
            }
    except Exception as e:
        logger.warning("[KUGOU] search/type=album error kw=%r: %s", kw, e)
        return {"lists": [], "total": 0, "page": page, "pagesize": limit, "keyword": kw}


async def get_album_songs(album_id: int | str, page: int = 1, pagesize: int = 50) -> dict:
    """调 KuGouMusicApi /album/songs?id=<album_id>&page=&pagesize=。

    pagesize 上限 50：超过即返回 {"errmsg":"invalid param","status":0} 空结果，
    是硬上限不是截断。这里主动封顶，避免飞牛侧传 size=120 触发无效请求。

    回参 data 真实字段（实测 12799221 / 182443696 一致）：
      { "songs": [嵌套结构], "total": n }
    songs[] 元素是嵌套结构，不是扁平字段：
      base.audio_name / base.author_name / base.album_id / base.audio_id
      authors[]: {author_name, author_id}   <- 带真实歌手 ID
      audio_info.duration(ms) / .bitrate / .filesize / .extname / .hash
                / .hash_320 / .hash_128 / .hash_flac
      album_info.album_name / album_info.cover(带 {size} 占位符)
      trans_param.union_cover(带 {size} 占位符) / trans_param.songname_suffix(如 "(伴奏)")
      extend.disc / extend.sort

    不能复用 search_item_to_raw：它按扁平字段读（name/singername/timelength/albumid），
    嵌套结构全读空；字段名与口径也不同。这里自己映射成 /artist/audios 口径的统一
    内部格式，再交给 build_online_track。

    兼容其他可能形态：data.songList / data.list / data.lists / 扁平字段兜底。
    """
    aid = str(album_id or "").strip()
    if not aid:
        return {"items": [], "total": 0, "page": page, "pagesize": pagesize, "album_id": ""}
    if pagesize > 50:
        pagesize = 50
    empty = {"items": [], "total": 0, "page": page, "pagesize": pagesize, "album_id": aid}
    try:
        async with _client() as c:
            r = await c.get("/album/songs",
                            params={"id": aid, "page": page, "pagesize": pagesize})
            if r.status_code != 200:
                logger.warning("[KUGOU] album/songs http=%s album=%s body=%r",
                               r.status_code, aid, r.text[:200])
                return empty
            body = r.json()
            errmsg = body.get("errmsg") or body.get("error_msg") or ""
            # 先 error_code 后 status（status=0 是失败态），见 envelope_state 注释
            if envelope_state(body) != "valid":
                logger.warning("[KUGOU] album/songs %s album=%s errmsg=%r",
                               envelope_detail(body), aid, errmsg)
                return empty
            data = body.get("data") or {}
            # 嵌套结构：data.songs / data.songList / data.list / data.lists
            song_list = data.get("songs") or data.get("songList") or data.get("songlist") or []
            if not isinstance(song_list, list) or not song_list:
                song_list = data.get("list") or data.get("lists") or []
            song_list = [x for x in song_list if isinstance(x, dict)] if isinstance(song_list, list) else []
            total = _to_int(data.get("total") or 0)
            if total <= 0:
                total = len(song_list)
            # pagesize 超限时酷狗返 errmsg="invalid param" 并空结果。
            if not song_list and errmsg:
                logger.warning("[KUGOU] album/songs empty errmsg=%r album=%s pagesize=%d",
                               errmsg, aid, pagesize)
            logger.warning("[KUGOU] album/songs album=%s got=%d total=%d errmsg=%r",
                           aid, len(song_list), total, errmsg)
    except Exception as e:
        logger.warning("[KUGOU] album/songs error album=%s: %s", aid, e)
        return empty

    items: list[dict] = []
    for it in song_list:
        # 嵌套结构优先：base / audio_info / album_info / authors / trans_param / extend
        base = it.get("base") or {}
        audio_info = it.get("audio_info") or {}
        album_info = it.get("album_info") or {}
        tp = it.get("trans_param") or {}
        # 歌手：authors[] 带真实 author_id，可直接生成 artist guid。
        authors_raw = it.get("authors") or []
        singers: list[dict] = []
        if isinstance(authors_raw, list) and authors_raw:
            for a in authors_raw:
                if not isinstance(a, dict):
                    continue
                nm = str(a.get("author_name") or a.get("name") or "").strip()
                if not nm:
                    continue
                ai_s = str(a.get("author_id") or a.get("id") or "").strip()
                singers.append({"name": nm, "id": ai_s})
        if not singers:
            singer_name = str(it.get("singername") or base.get("author_name") or "").strip()
            singers = [
                {"name": part.strip(), "id": ""}
                for part in re.split(r"[、,，/]", singer_name) if part.strip()
            ]
        singer_name = "、".join(str(s.get("name") or "").strip() for s in singers
                                if str(s.get("name") or "").strip())

        raw_name = str(base.get("audio_name") or it.get("name") or "").strip()
        album_name = str(album_info.get("album_name") or it.get("albumname")
                         or it.get("album_name") or "").strip()
        title = _strip_singer_prefix_from_name(raw_name, [singer_name] if singer_name else [], album_name)
        # 曲名后缀（如 "(伴奏)"）拼在曲名后，供伴奏版本区分。
        suffix = str(tp.get("songname_suffix") or "").strip()
        if suffix and suffix not in title:
            title = f"{title} {suffix}"

        pref = str(CFG.get("kugou_quality") or "high").strip().lower()
        quality_key = {
            "high": "high",
            "flac": "flac",
            "320": "320",
            "128": "128",
        }.get(pref, "high")

        sid = str(
            audio_info.get(f"hash_{quality_key}")
            or audio_info.get("hash_flac")
            or audio_info.get("hash_320")
            or audio_info.get("hash_128")
            or audio_info.get("hash")
            or base.get("audio_id")
            or it.get("hash")
            or it.get("filehash")
            or ""
        ).strip()
        if not sid:
            continue

        duration = _to_float(
            audio_info.get(f"duration_{quality_key}")
            or audio_info.get("duration_flac")
            or audio_info.get("duration_320")
            or audio_info.get("duration_128")
            or audio_info.get("duration")
            or 0
        )

        bitrate = _to_int(
            audio_info.get(f"bitrate_{quality_key}")
            or audio_info.get("bitrate_high")
            or audio_info.get("bitrate")
            or 0
        )
        file_size = _to_int(
            audio_info.get(f"filesize_{quality_key}")
            or audio_info.get("filesize_flac")
            or audio_info.get("filesize_320")
            or audio_info.get("filesize_128")
            or audio_info.get("filesize")
            or 0
        )
        ext = "flac" if quality_key in {"flac", "high"} else "mp3"
        cover = str(album_info.get("cover") or tp.get("union_cover")
                    or it.get("image") or "").strip()

        items.append({
            "id": f"kugou:{sid}",
            "source": "kugou",
            "hash": sid,
            "title": title,
            "artist": singer_name,
            "artists": singers,
            "album": album_name,
            "album_id": str(base.get("album_id") or it.get("albumid") or aid).strip(),
            "duration_s": duration,
            "ext": ext,
            "cover_url": cover,
            "union_cover": cover,
            "file_size": file_size,
            "bitrate": bitrate,
            "lyric": str(it.get("lyric") or "").strip(),
            "release_date": str(it.get("publish_time") or it.get("publish_date") or "").strip(),
            "disc_no": _to_int((it.get("extend") or {}).get("disc") or 0) or None,
            "track_no": _to_int((it.get("extend") or {}).get("sort") or 0) or None,
        })
    return {"items": items, "total": total, "page": page, "pagesize": pagesize, "album_id": aid}

async def get_album_detail_bundle(album_id: int | str) -> dict:
    """/album/detail 聚合：detail 拿歌手名+专辑名 → search?type=album → 按 albumid 匹配。

    先取 detail 是因为 /search 的 keywords 必须有歌手名和专辑名，而这两个
    字段 /album/detail?id=<id> 已经给全（author_name + album_name），不需要外部传入。

    返回 {"detail": <酷狗 detail 原样>, "match": <search 命中的原始条目或 None>}。
    detail 与 search 串行依赖（keywords 来自 detail），无法并行。
    """
    aid = str(album_id or "").strip()
    if not aid:
        return {"detail": {}, "match": None, "album_id": ""}

    now = time.monotonic()
    cached = _ALBUM_DETAIL_CACHE.get(aid)
    if cached and now - cached[0] < _ALBUM_DETAIL_TTL_S:
        return cached[1]

    detail = await get_album_detail(aid)
    author = str(detail.get("author_name") or detail.get("authorName") or "").strip()
    name = str(detail.get("album_name") or detail.get("albumName") or detail.get("album") or "").strip()
    if not (author or name):
        logger.warning("[KUGOU_ALBUM_DETAIL] empty name album_id=%s detail=%r", aid, str(detail)[:150])
        return {"detail": detail, "match": None, "album_id": aid}

    # 关键词：优先「歌手+专辑名」拼接。实测只搜专辑名会撞同名专辑
    # （keywords=小心思 → total=500 且首位不是本专辑）；带上歌手名 total=1 精准命中。
    candidates = []
    kw = author + name
    if kw:
        candidates.append(kw)
    if f"{author} {name}" and f"{author} {name}" not in candidates:
        candidates.append(f"{author} {name}")
    if name and name not in candidates:
        candidates.append(name)

    match = None
    for kw in candidates:
        res = await search_albums_raw(kw, limit=30)
        lists = res.get("lists") or []
        hit = next((x for x in lists if str(x.get("albumid")) == aid), None)
        if hit:
            match = hit
            break
        # 只有单一候选且未命中时才继续尝试；避免多候选串行拖慢失败路径。
        if len(candidates) <= 1:
            break

    out = {"detail": detail, "match": match, "album_id": aid}
    _ALBUM_DETAIL_CACHE[aid] = (now, out)
    return out


async def get_album_detail(album_id: int | str) -> dict:
    """调 KuGouMusicApi /album/detail，返回专辑详情。

    接口：/album/detail?id=180668003
    回参：{"status": 1, "error_code": 0, "errmsg": "", "data": [ {...} ]}

    data[] 单条字段（酷狗原始输出）：
      album_id / album_name / sizable_cover(带 {size} 占位符)
      cover(仅文件名，不能用) / author_name / authors[]
      publish_date / language / type / publish_company / heat / category / intro
    """
    aid = str(album_id or "").strip()
    if not aid:
        return {}
    try:
        async with _client() as c:
            r = await c.get("/album/detail", params={"id": aid})
            if r.status_code != 200:
                logger.warning("[KUGOU] album/detail http=%s album_id=%s body=%r",
                               r.status_code, aid, r.text[:200])
                return {}
            body = r.json()
            if not isinstance(body, dict):
                logger.warning("[KUGOU] album/detail body type=%s album_id=%s body=%r",
                               type(body).__name__, aid, str(body)[:200])
                return {}
            # 先 error_code 后 status（status=0 是失败态），见 envelope_state 注释
            if envelope_state(body) != "valid":
                logger.warning("[KUGOU] album/detail %s album_id=%s errmsg=%r",
                               envelope_detail(body), aid, body.get("errmsg"))
                return {}
            data = body.get("data") or []
            if isinstance(data, dict):
                data = data.get("list") or data.get("items") or []
            if not isinstance(data, list) or not data:
                logger.warning("[KUGOU] album/detail empty data album_id=%s body=%r", aid, str(body)[:200])
                return {}
            first = data[0]
            return first if isinstance(first, dict) else {}
    except Exception as e:
        logger.warning("[KUGOU] album/detail error album_id=%s: %s", aid, e)
        return {}


async def get_artist_albums(artist_id: int | str, page: int = 1, pagesize: int = 60) -> dict:
    """调 KuGouMusicApi /artist/albums，返回该歌手真实专辑列表。

    接口：/artist/albums?id=6539&page=1&pagesize=60
    回参：{"total": n, "error_code": 0, "status": 1, "data": [...],
           "extra": {"page_total": n}, "errmsg": "success"}

    data[] 真实字段（酷狗原始输出，与 /artist/audios 不同）：
      album_id / album_name / author_name(可能多人，顿号分隔)
      authors: [{"author_name": 名, "author_id": 数字ID}]  ← 带真实歌手 ID
      sizable_cover(带 {size} 占位符) / cover(文件名)
      publish_date(YYYY-MM-DD) / language / type(单曲专辑|原声带|录音室专辑|...)
      heat / category / grade / quality / is_publish / publish_company / intro
    """
    aid = str(artist_id or "").strip()
    if not aid:
        return {"items": [], "total": 0, "page": page, "pagesize": pagesize}
    try:
        async with _client() as c:
            r = await c.get(
                "/artist/albums",
                params={"id": aid, "page": page, "pagesize": pagesize},
            )
            if r.status_code != 200:
                logger.warning("[KUGOU] artist/albums http=%s aid=%s body=%r", r.status_code, aid, r.text[:200])
                return {"items": [], "total": 0, "page": page, "pagesize": pagesize}
            body = r.json()
            if not isinstance(body, dict):
                logger.warning("[KUGOU] artist/albums body type=%s aid=%s body=%r", type(body).__name__, aid, str(body)[:200])
                return {"items": [], "total": 0, "page": page, "pagesize": pagesize}
            # 先 error_code 后 status（status=0 是失败态），见 envelope_state 注释
            if envelope_state(body) != "valid":
                logger.warning("[KUGOU] artist/albums %s aid=%s errmsg=%r", envelope_detail(body), aid, body.get("errmsg"))
                return {"items": [], "total": 0, "page": page, "pagesize": pagesize}
            data = body.get("data") or []
            if isinstance(data, dict):
                data = data.get("list") or data.get("items") or []
            raw_items = data if isinstance(data, list) else []
            items: list[dict] = []
            for it in raw_items:
                if not isinstance(it, dict):
                    continue
                item = album_item_to_raw(it)
                if not item:
                    continue
                items.append(item)
            try:
                total = int(body.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            if not total:
                extra = body.get("extra") or {}
                if isinstance(extra, dict):
                    try:
                        total = int(extra.get("page_total") or 0)
                    except (TypeError, ValueError):
                        total = 0
            return {
                "items": items,
                "total": max(total, len(items)),
                "page": page,
                "pagesize": pagesize,
                "artist_id": aid,
            }
    except Exception as e:
        logger.warning("[KUGOU] artist/albums error aid=%s: %s", aid, e)
        return {"items": [], "total": 0, "page": page, "pagesize": pagesize}


def album_item_to_raw(it: dict) -> dict:
    """把 /artist/albums 单条专辑映射为内部统一格式。

    与 track_item_to_raw 不同：这里 authors 是数组且带真实 author_id，
    歌手 ID 不用再反查。
    """
    if not isinstance(it, dict):
        return {}
    album_id = _pick_ci(it, "album_id", "albumId", "AlbumID", "albumID")
    album_name = _pick_ci(it, "album_name", "albumName", "albumname", "AlbumName")
    if not album_id and not album_name:
        return {}

    # authors 数组优先（带 author_id）；回退到 author_name 字符串拆分。
    artists: list[dict] = []
    seen_names: set[str] = set()
    authors = it.get("authors")
    if isinstance(authors, list):
        for au in authors:
            if not isinstance(au, dict):
                continue
            name = str(au.get("author_name") or au.get("authorName") or au.get("name") or "").strip()
            if not name or name in seen_names:
                continue
            author_id = str(au.get("author_id") or au.get("authorId") or au.get("AuthorID") or "").strip()
            artists.append({"name": name, "id": author_id})
            seen_names.add(name)
    if not artists:
        singer_text = _pick_ci(it, "author_name", "authorName", "singername", "singername",
                               "SingerName", "artist", "author")
        for name in _split_singers(singer_text):
            if name and name not in seen_names:
                artists.append({"name": name, "id": ""})
                seen_names.add(name)

    # 封面：sizable_cover 带 {size} 占位符；顶层 cover 只是文件名，不能用。
    cover_url = _pick_ci(it, "sizable_cover", "sizableCover", "SizableCover",
                         "sizable_cover_url", "coverUrl", "Image", "Image_300x300")
    if not cover_url:
        cover_url = _pick_ci(it, "cover", "pic", "avatar")

    return {
        "id": f"kugou:album:{album_id}" if album_id else "",
        "source": "kugou",
        "album_id": album_id,
        "album": album_name,
        "album_name": album_name,
        "artists": artists,
        "artist": "、".join(a["name"] for a in artists if a["name"]),
        "cover_url": cover_url,
        "union_cover": cover_url,
        "publish_date": _pick_ci(it, "publish_date", "publishDate", "PublishDate", "pub_date"),
        "language": _pick_ci(it, "language", "Language"),
        "album_type": _pick_ci(it, "type", "album_type", "albumType"),
        "publish_company": _pick_ci(it, "publish_company", "publishCompany", "company"),
        "heat": _to_int(it.get("heat") or 0),
        "category": _to_int(it.get("category") or 0),
        "intro": _pick_ci(it, "intro", "album_intro", "desc", "description"),
        "is_publish": _to_int(it.get("is_publish") if it.get("is_publish") is not None else 1),
    }


def _split_singers(text: str) -> list[str]:
    """拆分酷狗顿号/逗号分隔的多歌手字符串。"""
    out: list[str] = []
    for part in re.split(r"[、,，/]+", str(text or "")):
        name = part.strip()
        if name:
            out.append(name)
    return out


def _field(it: dict, keys: tuple[str, ...], default: Any = "") -> Any:
    """按 KuGouMusicApi 常见大小写/命名兼容取字段。"""
    for k in keys:
        if it.get(k) not in (None, ""):
            return it.get(k)
    return default


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0






def _album_info_id(albuminfo: Any) -> str:
    """酷狗歌单 track/all 正确格式：albuminfo.id。"""
    if isinstance(albuminfo, dict):
        return str(albuminfo.get("id") or albuminfo.get("albumid") or albuminfo.get("albumId") or "").strip()
    return ""


def _album_name_from_albuminfo(albuminfo: Any) -> str:
    """酷狗歌单 track/all 正确格式：albuminfo.name。"""
    if isinstance(albuminfo, dict):
        return str(albuminfo.get("name") or "").strip()
    return ""


def _timelen_to_seconds(value: Any) -> float:
    """酷狗 timelen 直接返回原始数值，不做单位换算。"""
    return _to_float(value)




def _strip_singer_prefix_from_name(name: str, singer_names: list[str], album_name: str) -> str:
    """去掉酷狗 song name 里的歌手前缀：例如 "球球、白小白 - 宠爱吖" -> "宠爱吖"。"""
    title = str(name or "").strip()
    if not title:
        return title
    # 酷狗常见格式：歌手、歌手 - 歌名；先按最后一个 " - " 切分，右侧通常就是歌名。
    if " - " in title:
        right = title.rsplit(" - ", 1)[1].strip()
        left = title.rsplit(" - ", 1)[0].strip()
        if right and (not left or left == left):  # 只要右侧非空，优先采用右侧歌名
            return right
    names = [str(x or "").strip() for x in singer_names if str(x or "").strip()]
    if not names:
        return title
    joined = "、".join(names)
    prefixes = {
        joined,
        "/".join(names),
        ",".join(names),
        "，".join(names),
        " - ".join(names),
        f"{joined} - {album_name}",
        f"{joined}-{album_name}",
    }
    for sep in (" - ", "-", "_", "–", "—", "|", ";", "；"):
        for prefix in prefixes:
            if title.startswith(prefix + sep) or title.startswith(prefix):
                cleaned = title[len(prefix):].lstrip(sep).lstrip("-_|;； ").strip()
                if cleaned:
                    return cleaned
    return title


def track_item_to_raw(it: dict) -> dict:
    """按酷狗 /playlist/track/all 的真实返回格式精确映射。

    真实字段：hash/name/singerinfo[]/albuminfo/timelen/extname/size/cover/bitrate。
    """
    if not isinstance(it, dict):
        return {}

    sid = str(it.get("hash") or "").strip()
    if not sid:
        return {}

    artists = []
    if isinstance(it.get("singerinfo"), list):
        for singer in it.get("singerinfo"):
            if isinstance(singer, dict):
                singer_name = str(singer.get("name") or "").strip()
                if singer_name:
                    artists.append(singer)
    singer_names = [str(x.get("name") or "").strip() for x in artists]
    album = _album_name_from_albuminfo(it.get("albuminfo"))
    artist = "、".join(singer_names)
    title = _strip_singer_prefix_from_name(str(it.get("name") or ""), singer_names, album)
    info_obj = it.get("info")
    # ⚠️ timelen 的单位是**毫秒**（实测 /playlist/track/all：212898 = 3:32.898）。
    # 变量名 `duration_s` 是历史遗留（早期误以为是秒），**它的值恒为毫秒**，
    # 下游 build_online_track 的 audioSpec.duration 也要求毫秒
    # （飞牛 music.db 的 track.duration_ms 就是毫秒，见 trim.music/target/sqlite.sql）。
    # ⇒ 这里**绝不要**再除以 1000：那会让每首歌变成 0.2 秒左右。
    # 全工程只有两处需要秒→毫秒换算，都写了注释：
    #   search_item_to_raw 的 Duration（/search 回的是秒）与本文件 daily_item_to_raw 的 time_length。
    if isinstance(info_obj, dict):
        duration_s = _to_float(info_obj.get("timelen")) or _to_float(it.get("timelen"))
    else:
        duration_s = _to_float(it.get("timelen"))
    tier = _select_quality_tier(it)
    if tier:
        ext = tier["suffix"]
    else:
        ext = _format_from_any(it)
    cover = str(it.get("cover") or "").strip()
    if not cover:
        trans_param = it.get("trans_param")
        if isinstance(trans_param, dict):
            cover = str(trans_param.get("union_cover") or "").strip()
    if not cover and isinstance(info_obj, dict):
        cover = str(info_obj.get("image") or info_obj.get("cover") or "").strip()
    file_size = _to_int((info_obj or {}).get("filesize")) or _to_int((info_obj or {}).get("size")) or _to_int(it.get("size"))
    bitrate = _to_int((info_obj or {}).get("bitrate")) or _to_int(it.get("bitrate"))
    if tier:
        if tier["bitrate"]:
            bitrate = tier["bitrate"]
        if tier["size"]:
            file_size = tier["size"]

    return {
        "id": f"kugou:{sid}",
        "source": "kugou",
        "hash": sid,
        "title": title,
        "artist": artist,
        "artists": artists,
        "album": album,
        "album_id": _album_info_id(it.get("albuminfo")),
        "duration_s": duration_s,
        "ext": ext,
        "cover_url": cover,
        "union_cover": cover,
        "file_size": file_size,
        "bitrate": bitrate,
        "lyric": "",
        **({"_matched_tier": tier} if tier else {}),
    }

# ============================================================
# 音频流 URL
# ============================================================

QUALITY_FALLBACK = ["high", "flac", "320", "128"]

# relate_goods[].level -> 配置音质值 映射表
# level 2=128kbps mp3, 4=320kbps mp3, 5=flac(无损), 6=flac(Hi-Res)
# suffix 是输出格式后缀（mp3/flac），quality 是 /song/url 请求参数
QUALITY_TIERS = [
    {"level": 2, "quality": "128",  "suffix": "mp3"},
    {"level": 4, "quality": "320",  "suffix": "mp3"},
    {"level": 5, "quality": "flac", "suffix": "flac"},
    {"level": 6, "quality": "high", "suffix": "flac"},
]
QUALITY_TIERS_DESC = list(reversed(QUALITY_TIERS))


def _pick_relate_quality_fields(item: dict) -> tuple[int, int]:
    """从 relate_goods 单项里读取码率与文件大小。

    线上真实结构在 item.info 下；保留顶层兼容，避免旧回包失效。
    """
    bitrate = 0
    size = 0
    info_obj = item.get("info")
    if isinstance(info_obj, dict):
        bitrate = _to_int(info_obj.get("bitrate") or 0)
        size = _to_int(info_obj.get("filesize") or info_obj.get("size") or 0)
    if not bitrate:
        bitrate = _to_int(item.get("bitrate") or 0)
    if not size:
        size = _to_int(item.get("filesize") or item.get("size") or 0)
    return bitrate, size


def _parse_relate_goods(data: dict) -> dict[int, dict]:
    """解析 data.relate_goods[] 为 {level: item_dict} 索引。"""
    rel = data.get("relate_goods") if isinstance(data, dict) else None
    if not isinstance(rel, list):
        return {}
    out: dict[int, dict] = {}
    for it in rel:
        if not isinstance(it, dict):
            continue
        try:
            lvl = int(it.get("level"))
        except (TypeError, ValueError):
            continue
        out[lvl] = it
    return out


def _select_quality_tier(data: dict) -> dict | None:
    """从 relate_goods[] 匹配音质档位。

    1) 精确匹配：配置的 kugou_quality 在 relate_goods 里存在对应 level
    2) 降级匹配：配置音质不在结果中时，从高到低在候选范围内取首个可用 level
    返回 {level, quality, suffix, bitrate, size, match}，无 relate_goods 时返回 None。
    """
    by_level = _parse_relate_goods(data)
    if not by_level:
        return None
    pref = str(CFG.get("kugou_quality") or "high").strip().lower()
    for tier in QUALITY_TIERS_DESC:
        if tier["quality"] != pref:
            continue
        item = by_level.get(tier["level"])
        if item is None:
            continue
        bitrate, size = _pick_relate_quality_fields(item)
        return {**tier, "bitrate": bitrate, "size": size, "match": "exact"}
    pref_idx = None
    for idx, tier in enumerate(QUALITY_TIERS_DESC):
        if tier["quality"] == pref:
            pref_idx = idx
            break
    candidates = QUALITY_TIERS_DESC[pref_idx:] if pref_idx is not None else QUALITY_TIERS_DESC
    # 降级：从配置的档位开始，继续按高->低顺序取首个可用
    for tier in candidates:
        item = by_level.get(tier["level"])
        if item is None:
            continue
        bitrate, size = _pick_relate_quality_fields(item)
        return {**tier, "bitrate": bitrate, "size": size, "match": "downgrade"}
    return None

async def resolve_url(song_id: str, quality_tier: dict | None = None) -> tuple[str | None, str | None]:
    """按音质档位尝试解析播放直链。

    返回 (url, ext)：解析失败返回 (None, None)。
    quality_tier: 匹配好的 {quality, suffix, ...}；None 则用 CFG 默认档位。
    song_id 为 "hash" 部分，不含 "kugou:" 前缀。
    """
    if not song_id:
        return None, None
    pref = str(quality_tier["quality"]) if quality_tier else str(CFG.get("kugou_quality") or "high")
    order = [pref] + [q for q in QUALITY_FALLBACK if q != pref]
    tier_suffix = str(quality_tier.get("suffix") or "") if quality_tier else ""
    logger.warning("[KUGOU] resolve_url hash=%s order=%s suffix=%s", song_id, order, tier_suffix)

    async with _client() as c:
        for q in order:
            try:
                r = await c.get(
                    "/song/url",
                    params={"hash": song_id, "quality": q},
                )
                body_preview = r.text[:500]
                if r.status_code != 200:
                    logger.warning("[KUGOU] resolve_url hash=%s q=%s http=%s body=%r",
                                   song_id, q, r.status_code, body_preview)
                    continue
                data = r.json()
                # 兼容 API 返回字符串的情况
                if isinstance(data, str):
                    logger.warning("[KUGOU] resolve_url hash=%s q=%s response is str: %r",
                                   song_id, q, data[:200])
                    continue
                if not isinstance(data, dict):
                    logger.warning("[KUGOU] resolve_url hash=%s q=%s response type=%s body=%r",
                                   song_id, q, type(data).__name__, body_preview)
                    continue
                status = data.get("status")
                if status != 1:
                    logger.warning("[KUGOU] resolve_url hash=%s q=%s status=%s body=%r",
                                   song_id, q, status, body_preview)
                    continue
                # 兼容多种返回结构：优先顶层 url，其次 backupUrl，最后嵌套 data.url / data.backupUrl
                url = None
                ext = "mp3"
                if isinstance(data.get("url"), list) and data["url"]:
                    url = data["url"][0]
                    ext = str(data.get("extName") or "mp3").lower()
                elif isinstance(data.get("url"), str):
                    url = data["url"]
                    ext = str(data.get("extName") or "mp3").lower()
                elif isinstance(data.get("backupUrl"), list) and data["backupUrl"]:
                    url = data["backupUrl"][0]
                    ext = str(data.get("extName") or "mp3").lower()
                elif isinstance(data.get("backupUrl"), str):
                    url = data["backupUrl"]
                    ext = str(data.get("extName") or "mp3").lower()
                elif isinstance(data.get("data"), dict):
                    d2 = data["data"]
                    if isinstance(d2.get("url"), list) and d2["url"]:
                        url = d2["url"][0]
                        ext = str(d2.get("extName") or "mp3").lower()
                    elif isinstance(d2.get("url"), str):
                        url = d2["url"]
                        ext = str(d2.get("extName") or "mp3").lower()
                    elif isinstance(d2.get("backupUrl"), list) and d2["backupUrl"]:
                        url = d2["backupUrl"][0]
                        ext = str(d2.get("extName") or "mp3").lower()
                    elif isinstance(d2.get("backupUrl"), str):
                        url = d2["backupUrl"]
                        ext = str(d2.get("extName") or "mp3").lower()
                if not url:
                    logger.warning("[KUGOU] resolve_url hash=%s q=%s no url body=%r",
                                   song_id, q, body_preview)
                    continue
                logger.warning("[KUGOU] resolve_url OK hash=%s q=%s ext=%s url=%s...",
                               song_id, q, ext, url[:100])
                return url, ext
            except Exception as e:
                logger.warning("[KUGOU] resolve_url hash=%s q=%s err=%s", song_id, q, e)
    logger.warning("[KUGOU] resolve_url ALL FAILED hash=%s", song_id)
    return None, None


# ============================================================
# 歌曲信息（用于 track/metadata）
# ============================================================

async def get_user_playlists(page: int = 1, pagesize: int = 500) -> dict:
    """拉取 KuGouMusicApi 当前登录用户的歌单列表。

    统一返回 {"items": [...], "total": n, "page": p, "pagesize": s}。
    字段名尽量同时兼容 data.list/data.list[]/data.items[]/lists[]。
    """
    try:
        async with _client() as c:
            r = await c.get(
                "/user/playlist",
                params={"page": page, "pagesize": pagesize},
            )
            if r.status_code != 200:
                logger.warning("[KUGOU] user_playlist http=%s body=%r", r.status_code, r.text[:200])
                return {"items": [], "total": 0, "page": page, "pagesize": pagesize}
            data = r.json()
            st = data.get("status", data.get("code"))
            if st not in (1, 200, 0):
                logger.warning("[KUGOU] user_playlist status=%s body=%r", st, data)
                return {"items": [], "total": 0, "page": page, "pagesize": pagesize}

            raw = data.get("data") or {}
            raw_list = None
            if isinstance(raw, list):
                raw_list = raw
            elif isinstance(raw, dict):
                # KuGouMusicApi 返回格式: data.info / data.list / data.items / data.lists / data.records
                for key in ("info", "list", "items", "lists", "data", "records"):
                    v = raw.get(key)
                    if isinstance(v, list):
                        raw_list = v
                        break
            items = [x for x in (raw_list or []) if isinstance(x, dict)]
            total = int(raw.get("total") or raw.get("count") or len(items)) if isinstance(raw, dict) else len(items)
            logger.warning("[KUGOU] user_playlist page=%d pagesize=%d items=%d total=%d", page, pagesize, len(items), total)
            return {"items": items, "total": total, "page": page, "pagesize": pagesize}
    except Exception as e:
        logger.warning("[KUGOU] user_playlist error: %s", e)
        return {"items": [], "total": 0, "page": page, "pagesize": pagesize}


# ============================================================
# 登录态探活（P-07）
# ============================================================
# 只判「有没有存 token」不够：token 服务端失效后界面仍显示已登录，
# 用户只能靠「歌单怎么空了」自己发现。这里做一次轻量探活。
_LOGIN_PROBE_TTL_S = 600.0          # valid / invalid 缓存 10 分钟
_LOGIN_PROBE_TTL_UNKNOWN_S = 30.0   # 没结论只压 30 秒：上游一恢复就能反映
_LOGIN_PROBE_TIMEOUT_S = 5.0        # 固定 5s，**不跟** kugou_search_timeout(15s) 走
_login_probe_cache: dict[str, Any] = {"at": 0.0, "ttl": 0.0, "result": None}


def invalidate_login_probe() -> None:
    """清掉探活缓存。扫码成功 / 退出登录后必须调用，否则最多 10 分钟内还是旧结论。"""
    _login_probe_cache.update({"at": 0.0, "ttl": 0.0, "result": None})
    logger.info("[KUGOU] login_probe cache invalidated")


async def probe_login_state(force: bool = False) -> dict:
    """探活一次登录态，返回 {"state", "status", "error_code", "detail", "checked_at", "cached"}。

    state 取值：
      valid   —— error_code==0 且 status==1（信封正常）
      invalid —— error_code in (20010, 20017, 20018)，即**凭证不可用**
                 （token 失效、token 格式错、userid 缺失/不存在都算）
      unknown —— 上游超时/不可达/HTTP≠200/JSON 坏/status=0 但码值不认识。
                 **绝不能把 unknown 当失效**，否则会把用户推去白扫一次码。
      skipped —— kugou_enabled=0，不发请求
    """
    now = time.monotonic()
    cached = _login_probe_cache["result"]
    if not force and cached is not None and (now - _login_probe_cache["at"]) < _login_probe_cache["ttl"]:
        return {**cached, "cached": True}

    if not CFG.get("kugou_enabled", True):
        # 配置层已确认禁用，没必要打上游；也不缓存（开关随时会变）
        return {"state": "skipped", "status": None, "error_code": None,
                "detail": "kugou disabled", "checked_at": time.time(), "cached": False}

    started = time.monotonic()
    st: Any = None
    ec: Any = None
    state = "unknown"
    detail = ""
    try:
        async with _client() as c:
            r = await c.get("/user/playlist", params={"page": 1, "pagesize": 1},
                            timeout=_LOGIN_PROBE_TIMEOUT_S)
        try:
            body = r.json()
        except Exception:
            body = None
        if r.status_code != 200:
            detail = f"http={r.status_code}"
        elif not isinstance(body, dict):
            detail = "bad json"
        else:
            st, ec = body.get("status"), body.get("error_code")
            state = envelope_state(body)
            detail = envelope_detail(body)
    except Exception as e:  # 超时 / 连不上 / DNS 等，一律 unknown
        detail = f"{type(e).__name__}: {e}"
    elapsed = time.monotonic() - started

    if state == "valid":
        logger.info("[KUGOU] login_probe valid (%s) elapsed=%.2fs", detail, elapsed)
        ttl = _LOGIN_PROBE_TTL_S
    elif state == "invalid":
        # 20017 也涵盖「userid 不对」，所以统一写 invalid credentials，别写 token invalid
        logger.info("[KUGOU] login_probe invalid credentials (%s) elapsed=%.2fs", detail, elapsed)
        ttl = _LOGIN_PROBE_TTL_S
    else:
        # unknown 必须留下原始码值，将来出现没见过的码才能被发现
        logger.info("[KUGOU] login_probe unknown status=%s error_code=%s detail=%s elapsed=%.2fs",
                    st, ec, detail, elapsed)
        ttl = _LOGIN_PROBE_TTL_UNKNOWN_S

    result = {"state": state, "status": st, "error_code": ec,
              "detail": detail, "checked_at": time.time()}
    _login_probe_cache.update({"at": time.monotonic(), "ttl": ttl, "result": result})
    return {**result, "cached": False}


async def _refresh_privilege_tier(song_id: str, base_info: dict) -> dict | None:
    """搜索结果缓存缺少音质档位时，补拉 /privilege/lite 并合并匹配结果。"""
    if not song_id:
        return None
    try:
        async with _client() as c:
            r = await c.get("/privilege/lite", params={"hash": song_id})
            if r.status_code != 200:
                return None
            body = r.json()
            status = body.get("status", body.get("code"))
            if status not in (1, 200, 0):
                return None
            data = body.get("data") or body
            if isinstance(data, list) and data and isinstance(data[0], dict):
                data = data[0]
            if not isinstance(data, dict):
                return None
            tier = _select_quality_tier(data)
            if not tier:
                return None
            info = dict(base_info or {})
            info["id"] = f"kugou:{song_id}"
            info["source"] = "kugou"
            info["ext"] = tier["suffix"]
            if tier["bitrate"]:
                info["bitrate"] = tier["bitrate"]
            if tier["size"]:
                info["file_size"] = tier["size"]
            info["filesize"] = tier["size"]
            info["_matched_tier"] = tier
            _remember_song(info)
            logger.warning("[KUGOU] refresh tier hash=%s tier=%s suffix=%s match=%s bitrate=%s size=%s",
                           song_id, tier["quality"], tier["suffix"], tier["match"],
                           tier["bitrate"], tier["size"])
            return info
    except Exception as e:
        logger.warning("refresh_privilege_tier %s err=%s", song_id, e)
    return None


async def get_info(song_id: str) -> dict | None:
    """从内存索引拿元数据。搜过的歌直接命中；未搜过则用 /privilege/lite?hash= 回源。"""
    if not song_id:
        return None
    cached = _search_index.get(song_id)
    if cached:
        info = dict(cached)
        # 搜索列表缓存没有 relate_goods 匹配结果时，补拉一次 privilege/lite 修正后缀、大小、码率。
        if not info.get("_matched_tier"):
            refreshed = await _refresh_privilege_tier(song_id, info)
            if refreshed:
                return refreshed
        return info
    # 未搜过（直接进入详情页），用 /privilege/lite?hash= 拉元数据
    lite_info = await fetch_privilege_lite_info(song_id)
    if lite_info:
        return lite_info
    return {
        "id": f"kugou:{song_id}",
        "source": "kugou",
        "title": "",
        "artist": "",
        "album": "",
        "duration_s": 0,
        "ext": "mp3",
        "cover_url": "",
        "file_size": 0,
        "bitrate": 0,
        "lyric": "",
    }


async def fetch_privilege_lite_info(song_id: str) -> dict | None:
    """从 /privilege/lite?hash= 拉一首歌的元数据并映射到统一格式。"""
    if not song_id:
        return None
    try:
        async with _client() as c:
            r = await c.get(
                "/privilege/lite",
                params={"hash": song_id},
            )
            if r.status_code == 200:
                body = r.json()
                status = body.get("status", body.get("code"))
                if status not in (1, 200, 0):
                    logger.warning("[KUGOU] privilege_lite status=%s hash=%s body=%r", status, song_id, str(body)[:300])
                    return None
                data = body.get("data") or body
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    data = data[0]
                if not isinstance(data, dict):
                    return None
                cover_url = ""
                info_obj = data.get("info")
                if isinstance(info_obj, dict):
                    cover_url = str(info_obj.get("image") or "").strip()
                    if not data.get("timelen"):
                        data["timelen"] = info_obj.get("timelen")
                # 1) 从 relate_goods[] 匹配音质档位，覆写 extname/quality/size/bitrate
                tier = _select_quality_tier(data)
                if tier:
                    data["extname"] = tier["suffix"]
                    data["quality"] = tier["quality"]
                    data["_matched_tier"] = tier
                    if tier["bitrate"]:
                        data["bitrate"] = tier["bitrate"]
                    if tier["size"]:
                        data["filesize"] = tier["size"]
                    logger.warning("[KUGOU] privilege_lite tier match hash=%s tier=%s suffix=%s match=%s bitrate=%s size=%s",
                                   song_id, tier["quality"], tier["suffix"], tier["match"],
                                   tier["bitrate"], tier["size"])
                # info.extname 是真正的格式名（"mp3"/"flac"）；
                # data.quality 是比特率（"128"/"320"），不能当格式。
                if info_obj.get("extname") and not data.get("extname"):
                    data["extname"] = info_obj["extname"]
                if info_obj.get("duration") and not data.get("duration"):
                    data["duration"] = info_obj.get("duration")
                tp = data.get("trans_param")
                if isinstance(tp, dict) and not cover_url:
                    cover_url = str(tp.get("union_cover") or "").strip()
                raw_item = search_item_to_raw(data)
                if raw_item:
                    if cover_url and not raw_item.get("cover_url"):
                        raw_item["cover_url"] = cover_url
                        raw_item["union_cover"] = cover_url
                    if tier:
                        raw_item["_matched_tier"] = tier
                    _remember_song(raw_item)
                    return raw_item
    except Exception as e:
        logger.warning("privilege_lite %s err=%s", song_id, e)
    return None


async def search_by_hash(song_id: str) -> dict | None:
    """用 hash 搜一首歌并返回统一格式（用于详情页首次进入）。"""
    if not song_id:
        return None
    try:
        async with _client() as c:
            r = await c.get(
                "/search",
                params={"hash": song_id, "page": 1, "pagesize": 1, "type": "song"},
            )
            if r.status_code != 200:
                return None
            data = r.json()
            lists = ((data.get("data") or {}).get("lists") or [])
            if not lists or not isinstance(lists[0], dict):
                return None
            it = lists[0]
            # 复用 search 内部的字段映射
            sid = str(
                it.get("hash") or it.get("songhash") or it.get("songHash")
                or it.get("FileHash") or it.get("fileHash") or song_id
            ).strip()
            title = str(
                it.get("songname") or it.get("songName") or it.get("FileName")
                or it.get("fileName") or it.get("OriSongName") or ""
            ).strip()
            artist = str(
                it.get("singername") or it.get("singerName") or it.get("SingerName")
                or it.get("singer") or ""
            ).strip()
            album = str(
                it.get("albumname") or it.get("albumName") or it.get("AlbumName")
                or it.get("album") or ""
            ).strip()
            duration_raw = (
                it.get("duration") or it.get("playTime") or it.get("Duration")
                or it.get("PlayTime") or 0
            )
            try:
                duration_s = float(duration_raw)
            except (TypeError, ValueError):
                duration_s = 0.0
            ext = "mp3"
            if it.get("ExtName") in ("flac", "ape", "wv", "ogg", "m4a"):
                ext = str(it.get("ExtName")).lower()
            cover = str(
                it.get("Image") or it.get("Img") or it.get("img")
                or it.get("AlbumImg") or it.get("albumImg")
                or it.get("albumimg") or it.get("album_img")
                or it.get("AlbumImage") or it.get("albumImage")
                or it.get("PicUrl") or it.get("picUrl")
                or ""
            )
            # 保留 {size} 占位符，供 static_cover 按请求 size 动态替换
            file_size = 0
            try:
                file_size = int(it.get("FileSize") or 0)
            except (TypeError, ValueError):
                file_size = 0
            bitrate = 0
            try:
                bitrate = int(it.get("Bitrate") or 0)
            except (TypeError, ValueError):
                bitrate = 0
            item = {
                "id": f"kugou:{sid}",
                "source": "kugou",
                "title": title,
                "artist": artist,
                "album": album,
                "duration_s": duration_s,
                "ext": ext,
                "cover_url": cover,
                "file_size": file_size,
                "bitrate": bitrate,
                "lyric": "",
            }
            _remember_song(item)
            return item
    except Exception as e:
        logger.warning("search_by_hash %s err=%s", song_id, e)
    return None


# ============================================================
# 歌词
# ============================================================

async def fetch_lyric(song_id: str) -> str:
    """从 KuGouMusicApi 拉在线歌曲歌词：先用 hash 找 id/accesskey，再取 LRC。"""
    if not song_id:
        return ""
    try:
        async with _client() as c:
            r1 = await c.get("/search/lyric", params={"hash": song_id})
            if r1.status_code != 200:
                logger.warning("kugou fetch_lyric search/lyric hash=%s http=%s body=%r", song_id, r1.status_code, r1.text[:300])
                return ""
            j1 = r1.json()
            cands = j1.get("candidates") or []
            if not cands:
                logger.warning("kugou fetch_lyric search/lyric empty hash=%s status=%s body=%r", song_id, j1.get("status"), j1)
                return ""
            first = cands[0]
            r2 = await c.get(
                "/lyric",
                params={
                    "id": first.get("id"),
                    "accesskey": first.get("accesskey"),
                    "fmt": "lrc",
                    "decode": "true",
                },
            )
            if r2.status_code != 200:
                logger.warning("kugou fetch_lyric lyric hash=%s http=%s body=%r", song_id, r2.status_code, r2.text[:300])
                return ""
            j2 = r2.json()
            text = str(j2.get("decodeContent") or j2.get("lyric") or "").strip()
            logger.warning("kugou fetch_lyric hash=%s lyrics_len=%d status=%s", song_id, len(text), j2.get("status"))
            return text
    except Exception as e:
        logger.warning("kugou fetch_lyric song_id=%s err=%s", song_id, e)
        return ""


# ============================================================
# QR 登录
# ============================================================

async def qr_key() -> str | None:
    """获取二维码 key。"""
    try:
        async with httpx.AsyncClient(
            base_url=CFG["kugou_url"],
            timeout=10.0,
        ) as c:
            r = await c.get("/login/qr/key")
            if r.status_code != 200:
                logger.warning("kugou qr_key http %s: %s", r.status_code, r.text[:300])
                return None
            data = r.json()
            ok = (data.get("code") == 200) or (data.get("status") == 1) or (data.get("status") == 0)
            if ok and isinstance(data.get("data"), dict):
                return data["data"].get("qrcode")
            logger.warning("kugou qr_key bad response: %s", json.dumps(data, ensure_ascii=False)[:400])
    except Exception as e:
        logger.warning("kugou qr_key error: %s", e)
    return None


async def qr_create(key: str) -> str | None:
    """生成二维码图片（base64）。"""
    if not key:
        return None
    try:
        async with httpx.AsyncClient(
            base_url=CFG["kugou_url"],
            timeout=10.0,
        ) as c:
            r = await c.get("/login/qr/create", params={"key": key, "qrimg": "true"})
            if r.status_code != 200:
                logger.warning("kugou qr_create http %s: %s", r.status_code, r.text[:300])
                return None
            data = r.json()
            ok = (data.get("code") == 200) or (data.get("status") == 1) or (data.get("status") == 0)
            if ok and isinstance(data.get("data"), dict):
                return data["data"].get("base64")
            logger.warning("kugou qr_create bad response: %s", json.dumps(data, ensure_ascii=False)[:400])
    except Exception as e:
        logger.warning("kugou qr_create error: %s", e)
    return None


async def qr_check(key: str) -> dict:
    """检查二维码扫描状态。
    
    返回:
    - status=0: 二维码过期
    - status=1: 等待扫码
    - status=2: 已扫码，等待确认
    - status=4: 登录成功，返回用户信息
    """
    import time
    try:
        async with httpx.AsyncClient(
            base_url=CFG["kugou_url"],
            timeout=10.0,
        ) as c:
            r = await c.get(
                "/login/qr/check",
                params={"key": key, "timestamp": int(time.time() * 1000)},
                headers={"Cache-Control": "no-cache"},
            )
            if r.status_code != 200:
                return {"status": -1, "message": f"http {r.status_code}"}
            data = r.json()
            # 兼容 code/status 两种返回
            st = data.get("status", data.get("code"))
            if st == 1 or st == 200:
                return data.get("data", {}) or {"status": int(st)}
            # 扫码确认状态也可能嵌在 data.status
            inner = data.get("data")
            if isinstance(inner, dict) and inner.get("status") is not None:
                inner["message"] = data.get("message", inner.get("message", ""))
                return inner
            return {"status": -1, "message": data.get("message", "unknown")}
    except Exception as e:
        logger.warning("kugou qr_check error: %s", e)
        return {"status": -1, "message": str(e)}


def save_credentials(user_data: dict) -> dict:
    """保存登录凭证到 CFG，返回凭证摘要。"""
    credentials = {
        "token": user_data.get("token", ""),
        "userid": str(user_data.get("userid", "")),
        "dfid": user_data.get("dfid", ""),
        "t1": user_data.get("t1", ""),
        "mid": user_data.get("mid", ""),
        "guid": user_data.get("guid", ""),
        "dev": user_data.get("serverDev", ""),
        "mac": user_data.get("mac", ""),
    }
    # 更新 CFG
    CFG["kugou_token"] = credentials["token"]
    CFG["kugou_userid"] = credentials["userid"]
    CFG["kugou_dfid"] = credentials["dfid"]
    CFG["kugou_t1"] = credentials["t1"]
    CFG["kugou_mid"] = credentials["mid"]
    CFG["kugou_guid"] = credentials["guid"]
    CFG["kugou_dev"] = credentials["dev"]
    CFG["kugou_mac"] = credentials["mac"]
    return credentials
# ============================================================
# F-01 歌单类内容源：每日推荐 / 排行榜 / 精选歌单
# ============================================================
# 三个源的回包形状**各不相同**，不能共用 track_item_to_raw：
#
#   精选歌单 /playlist/track/all  data.songs[]     扁平：hash/name/singerinfo/timelen
#   → 现有 track_item_to_raw 直接可用（本文件 1049 行）
#
#   每日推荐 /everyday/recommend  data.song_list[] 字段名全不同：
#     songname（不是 name/audio_name）/ author_name / album_name / album_id
#     hash_flac|hash_320|hash_128 / filesize_flac|320|128 / sizable_cover
#     **time_length 单位是「秒」**（238 = 3:58），timelength_320 同样是秒
#
#   排行榜   /rank/audio          data.songlist[]  hash 与时长都嵌在 audio_info 里：
#     audio_info.hash_128|320|flac / audio_info.duration_128|320|flac（毫秒）
#     audio_info.filesize_* / album_info.album_name / album_info.sizable_cover
#
# ⇒ 三个 adapter 各自映射成同一套内部 raw，交给 build_online_track 出飞牛结构。
#
# 【单位约定 · 2026-10-31 实测，勿改】
#   timelen / timelength / audio_info.duration_*  → **毫秒**
#   /search 的 Duration、每日推荐的 time_length  → **秒**（各自映射里 ×1000）
#   飞牛侧 music.db 的 track.duration_ms（trim.music/target/sqlite.sql:178）
#   明确写「单位毫秒」⇒ 本地曲目与在线曲目量纲一致，这里不要再做任何换算。
# ============================================================

# 配置音质 -> 该源里可用的档位链（从高到低回退）。
# 这三个源里**没有** hash_high（Hi-Res）字段，只有 128/320/flac，
# 所以 high 与 flac 落到同一档是**有意的降级**，不是 bug。
_FEED_HASH_TIERS = {
    "high": ("flac", "320", "128"),
    "flac": ("flac", "320", "128"),
    "320": ("320", "128"),
    "128": ("128",),
}
_FEED_SUFFIX = {"flac": "flac", "320": "mp3", "128": "mp3"}


def _feed_quality_chain() -> tuple[str, ...]:
    pref = str(CFG.get("kugou_quality") or "high").strip().lower()
    return _FEED_HASH_TIERS.get(pref) or _FEED_HASH_TIERS["high"]


def _feed_pick_tier(container: dict) -> tuple[str, str, int]:
    """按配置音质在 hash_<tier> / filesize_<tier> 里挑一档。

    返回 (tier, hash, filesize)。一个都没有时返回 ("", "", 0)，
    由调用方决定是否回退到其他字段名。
    """
    if not isinstance(container, dict):
        return "", "", 0
    for tier in _feed_quality_chain():
        h = str(container.get(f"hash_{tier}") or "").strip()
        if h:
            return tier, h, _to_int(container.get(f"filesize_{tier}") or 0)
    return "", "", 0


def _feed_artists(it: dict) -> list[dict]:
    """歌手数组：author_name 可能是多人（"、"/","/"&" 分隔）。"""
    singers = _singers_from_any(it)
    if not singers:
        name = str(it.get("author_name") or it.get("authorName") or "").strip()
        if name:
            singers = [{"name": p.strip(), "id": ""} for p in re.split(r"[、,，/&]", name) if p.strip()]
    return singers


def _feed_artist_name(singers: list[dict]) -> str:
    return "、".join(str(x.get("name") or "").strip() for x in singers if isinstance(x, dict) and x.get("name"))


def daily_item_to_raw(it: dict) -> dict:
    """每日推荐 data.song_list[] → 内部统一 raw。

    实测字段（/everyday/recommend，2026-10-31）：songname / author_name /
    album_name / album_id / hash_flac|320|128 / filesize_flac|320|128 /
    sizable_cover / **time_length（秒）**。

    ⚠️ time_length 是秒：直接当毫秒用会让飞牛显示 0.238 秒。
    这是全工程唯一一处「秒 → 毫秒」的换算点，改之前先看文件头的单位约定。
    """
    if not isinstance(it, dict):
        return {}
    tier, sid, filesize = _feed_pick_tier(it)
    if not sid:
        sid = str(it.get("hash") or it.get("hash_128") or "").strip()
        filesize = filesize or _to_int(it.get("file_size") or it.get("filesize_128") or 0)
    if not sid:
        return {}
    singers = _feed_artists(it)
    singer_name = _feed_artist_name(singers)
    album = str(it.get("album_name") or it.get("albumName") or "").strip()
    raw_name = str(it.get("songname") or it.get("ori_audio_name") or it.get("filename") or "").strip()
    title = _strip_singer_prefix_from_name(raw_name, [singer_name] if singer_name else [], album)
    # 秒 → 毫秒（唯一一处）
    duration_ms = _to_int(it.get("time_length") or it.get("timelength_320") or 0) * 1000
    ext = _FEED_SUFFIX.get(tier or "", "mp3")
    cover = str(it.get("sizable_cover") or it.get("cover") or "").strip()
    return {
        "id": f"kugou:{sid}",
        "source": "kugou",
        "hash": sid,
        "title": title or raw_name,
        "artist": singer_name,
        "artists": singers,
        "album": album,
        "album_id": str(it.get("album_id") or it.get("albumID") or "").strip(),
        "duration_s": float(duration_ms),   # 名字是历史遗留，值恒为**毫秒**
        "ext": ext,
        "cover_url": cover,
        "union_cover": cover,
        "file_size": filesize,
        "bitrate": _to_int(it.get("bitrate") or 0),
        "lyric": "",
        "release_date": str(it.get("publish_time") or it.get("publish_date") or "").strip(),
    }


def rank_item_to_raw(it: dict) -> dict:
    """排行榜 data.songlist[] → 内部统一 raw。

    实测字段（/rank/audio，2026-10-31）：hash 与时长都在 audio_info 里，
    顶层没有 hash —— 先判 hash 的通用映射会直接返回空。

      audio_info.hash_128|hash_320|hash_flac
      audio_info.duration_128|320|flac（**毫秒**，与 timelen 同量纲）
      audio_info.filesize_flac|320|128
      album_info.album_name / album_info.sizable_cover
      album_id / album_audio_id / author_name / songname
    """
    if not isinstance(it, dict):
        return {}
    audio = it.get("audio_info") if isinstance(it.get("audio_info"), dict) else {}
    alb = it.get("album_info") if isinstance(it.get("album_info"), dict) else {}
    tier, sid, filesize = _feed_pick_tier(audio)
    if not sid:
        sid = str(audio.get("hash") or it.get("hash") or "").strip()
        filesize = filesize or _to_int(audio.get("filesize") or audio.get("size") or 0)
    if not sid:
        return {}
    duration_ms = _to_int(
        audio.get(f"duration_{tier}") if tier else 0
    ) or _to_int(
        audio.get("duration_flac") or audio.get("duration_320")
        or audio.get("duration_128") or audio.get("duration") or 0
    )
    singers = _feed_artists(it)
    if not singers and isinstance(it.get("authors"), list):
        singers = [{"name": str(x.get("author_name") or x.get("name") or "").strip(),
                    "id": str(x.get("author_id") or x.get("id") or "").strip()}
                   for x in it.get("authors") if isinstance(x, dict)]
    singer_name = _feed_artist_name(singers)
    album = str(alb.get("album_name") or it.get("album_name") or "").strip()
    raw_name = str(it.get("songname") or it.get("audio_name") or "").strip()
    title = _strip_singer_prefix_from_name(raw_name, [singer_name] if singer_name else [], album)
    cover = str(alb.get("sizable_cover") or alb.get("cover") or "").strip()
    if not cover:
        cover = _cover_from_any(it)
    ext = _FEED_SUFFIX.get(tier or "", "mp3")
    bitrate = _to_int(audio.get(f"bitrate_{tier}") if tier else 0) or _to_int(audio.get("bitrate") or 0)
    return {
        "id": f"kugou:{sid}",
        "source": "kugou",
        "hash": sid,
        "title": title or raw_name,
        "artist": singer_name,
        "artists": singers,
        "album": album,
        "album_id": str(it.get("album_id") or alb.get("album_id") or "").strip(),
        "duration_s": float(duration_ms),   # 历史命名，值恒为**毫秒**
        "ext": ext,
        "cover_url": cover,
        "union_cover": cover,
        "file_size": filesize,
        "bitrate": bitrate,
        "lyric": "",
        "release_date": str(it.get("publish_time") or "").strip(),
    }


def special_item_to_raw(it: dict) -> dict:
    """精选歌单 data.songs[] → 内部统一 raw（形状与 /playlist/track/all 一致）。

    实测可用，直接复用 track_item_to_raw，保留同名入口只为三个源对称。
    """
    return track_item_to_raw(it)


async def _feed_get(path: str, params: dict | None, tag: str) -> dict | None:
    """F-01 三个源的统一取数外壳：统一日志 + 统一吞异常（列表页宁可空，不许 500）。"""
    try:
        async with _client() as c:
            r = await c.get(path, params=params or {})
            if r.status_code != 200:
                logger.warning("[KUGOU][%s] http=%s body=%r", tag, r.status_code, r.text[:200])
                return None
            body = r.json()
            if not isinstance(body, dict):
                logger.warning("[KUGOU][%s] body type=%s body=%r", tag, type(body).__name__, str(body)[:200])
                return None
            return body
    except Exception as e:
        logger.warning("[KUGOU][%s] error: %s", tag, e)
        return None


async def fetch_daily_recommend(page: int = 1, size: int = 30) -> dict:
    """每日推荐：GET /everyday/recommend。

    该接口**没有分页参数**，固定回 30 首，按 data.creation_date 每天变一次
    （所以缓存要按日期，别用固定 TTL 跨天）。/recommend/songs 与它同源
    （hash 序列 30/30 相同、顺序一致），因此**不另做第二个入口**。
    """
    empty = {"items": [], "total": 0, "page": 1, "pagesize": 30, "creation_date": ""}
    body = await _feed_get("/everyday/recommend", None, "FEED_DAILY")
    if not body:
        return empty
    data = body.get("data")
    creation = ""
    if isinstance(data, dict):
        raw = data.get("song_list") or data.get("songList") or data.get("list") or []
        creation = str(data.get("creation_date") or data.get("creationDate") or "")
    elif isinstance(data, list):
        raw = data
    else:
        return empty
    items = []
    for it in raw if isinstance(raw, list) else []:
        raw_item = daily_item_to_raw(it)
        if not raw_item:
            continue
        items.append(raw_item)
        _remember_song(raw_item)
    logger.warning("[KUGOU][FEED_DAILY] creation_date=%s got=%d", creation or "(none)", len(items))
    return {"items": items, "total": len(items), "page": 1, "pagesize": 30, "creation_date": creation}


async def fetch_rank_top() -> list[dict]:
    """排行榜入口用 /rank/top，**不是** /rank/list。

    实测（2026-10-31）：/rank/list 的 55 个 info[].id 当 rankid 传，抽样 6 个只有
    id=2(TOP500) 能出歌，其余全空；/rank/top 回的 rankid 才带满内容。
    另外 rankid **会随日期变化**（今天 6 个 = 84235/85432/33165/59896/33163/35811），
    所以必须每次实时取，**不要硬编码**。
    """
    body = await _feed_get("/rank/top", None, "FEED_RANK_TOP")
    if not body:
        return []
    data = body.get("data")
    raw = []
    if isinstance(data, dict):
        for key in ("list", "info", "data", "ranks", "rank_list"):
            v = data.get(key)
            if isinstance(v, list):
                raw = v
                break
    elif isinstance(data, list):
        raw = data
    out = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        rid = str(it.get("rankid") or it.get("rankId") or it.get("id") or "").strip()
        if not rid:
            continue
        out.append({
            "rankid": rid,
            "name": str(it.get("rankname") or it.get("name") or it.get("title") or "").strip(),
            "cover": str(it.get("imgurl") or it.get("img") or it.get("cover") or "").strip(),
            "count": _to_int(it.get("song_count") or it.get("count") or 0),
        })
    logger.warning("[KUGOU][FEED_RANK_TOP] n=%d ids=%s", len(out), [x["rankid"] for x in out])
    return out


async def fetch_rank_audio(rankid: int | str, page: int = 1, size: int = 100) -> dict:
    """榜内歌曲：GET /rank/audio?rankid=&page=&pagesize=（data.songlist[]）。"""
    rid = str(rankid or "").strip()
    empty = {"items": [], "total": 0, "page": page, "pagesize": size, "rankid": rid}
    if not rid:
        return empty
    body = await _feed_get("/rank/audio", {"rankid": rid, "page": page, "pagesize": size}, "FEED_RANK_AUDIO")
    if not body:
        return empty
    data = body.get("data") or {}
    raw = data.get("songlist") or data.get("songList") or data.get("list") or []
    items = []
    for it in raw if isinstance(raw, list) else []:
        raw_item = rank_item_to_raw(it)
        if not raw_item:
            continue
        items.append(raw_item)
        _remember_song(raw_item)
    total = _to_int(data.get("total") or 0) or len(items)
    logger.warning("[KUGOU][FEED_RANK_AUDIO] rankid=%s page=%s size=%s got=%d total=%s",
                   rid, page, size, len(items), total)
    return {"items": items, "total": total, "page": page, "pagesize": size, "rankid": rid}


async def fetch_top_playlists(page: int = 1, size: int = 40) -> dict:
    """精选歌单列表：GET /top/playlist?page=&pagesize=（data.special_list[]）。

    实测 pagesize 硬上限 40（传 100 也只回 40），翻页看 data.has_next。
    注意 ident 必须用 global_collection_id（collection_3_..._0），
    传 specialid 给 /playlist/track/all 会直接 502。
    """
    empty = {"items": [], "total": 0, "page": page, "pagesize": 40, "has_next": False}
    body = await _feed_get("/top/playlist", {"page": page, "pagesize": size}, "FEED_TOP_PLAYLIST")
    if not body:
        return empty
    data = body.get("data") or {}
    raw = data.get("special_list") or data.get("list") or []
    out = []
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        gcid = str(it.get("global_collection_id") or it.get("globalCollectionId") or "").strip()
        if not gcid:
            continue
        out.append({
            "ident": gcid,
            "name": str(it.get("specialname") or it.get("name") or "").strip(),
            "cover": str(it.get("imgurl") or it.get("cover") or "").strip(),
            "creator": str(it.get("nickname") or "").strip(),
            "count": _to_int(it.get("percount") or it.get("songcount") or 0),
        })
    logger.warning("[KUGOU][FEED_TOP_PLAYLIST] page=%s got=%d has_next=%s",
                   page, len(out), data.get("has_next"))
    return {"items": out, "total": len(out), "page": page, "pagesize": 40,
            "has_next": bool(data.get("has_next"))}


async def fetch_collection_tracks(gcid: int | str, page: int = 1, size: int = 100) -> dict:
    """精选歌单曲目：GET /playlist/track/all?id=<global_collection_id>&page=&pagesize=。

    实测 pagesize=300 也能一次拉回整张（214 首），但为了不给上游一次压太多，
    app 侧统一按 100 分页循环。
    """
    ident = str(gcid or "").strip()
    empty = {"items": [], "total": 0, "page": page, "pagesize": size, "collection_id": ident}
    if not ident:
        return empty
    body = await _feed_get("/playlist/track/all", {"id": ident, "page": page, "pagesize": size},
                           "FEED_COLLECTION_TRACKS")
    if not body:
        return empty
    data = body.get("data") or {}
    if isinstance(data, list):
        data = {"songs": data}
    raw = data.get("songs") or data.get("list") or data.get("info") or []
    items = []
    for it in raw if isinstance(raw, list) else []:
        raw_item = special_item_to_raw(it)
        if not raw_item:
            continue
        items.append(raw_item)
        _remember_song(raw_item)
    # 上游把总数放在 data.count（实测 collection_..._373_0 → count=126），
    # 而 data.total 不存在。只读 total 会让分页 total 退化成"本页条数"，
    # 连带 size=-1 的全量循环在第一页就以为拉完了（126 首只拿到 100）。
    total = (_to_int(data.get("count") or 0)
             or _to_int(data.get("total") or 0)
             or len(items))
    logger.warning("[KUGOU][FEED_COLLECTION_TRACKS] gcid=%s page=%s size=%s got=%d total=%s",
                   ident, page, size, len(items), total)
    return {"items": items, "total": total, "page": page, "pagesize": size, "collection_id": ident}
