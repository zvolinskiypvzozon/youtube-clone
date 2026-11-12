import { api } from "./typed";
import type { PlaylistCreateIn, PlaylistUpdateIn } from "./types";

export const PLAYLIST_TITLE_MAX = 100;
export const PLAYLIST_DESCRIPTION_MAX = 5000;
export const PLAYLISTS_PAGE_SIZE = 20;
export const CHANNEL_VIDEOS_PAGE_SIZE = 24;

// Ключи — верхнего уровня, не вложены в ["channel", handle]: отмена/сброс карточки канала
// (подписка) сравнивает ключи по префиксу и задела бы списки.
export const playlistKey = (id: string) => ["playlist", id] as const;
export const playlistVideosKey = (id: string) => ["playlist-videos", id] as const;
export const myPlaylistsKey = (videoId?: string) => ["playlists", "me", videoId ?? null] as const;
export const channelPlaylistsKey = (handle: string) => ["channel-playlists", handle] as const;
export const channelVideosKey = (handle: string) => ["channel-videos", handle] as const;

/** Всё, что показывает плейлисты или их состав, — для инвалидации после изменений. */
export const PLAYLIST_LIST_KEYS = [["playlist"], ["playlist-videos"], ["playlists"], ["channel-playlists"]] as const;

const pl = (playlist_id: string) => ({ params: { playlist_id } });

export const fetchChannelVideos = (handle: string, cursor: string | null, signal?: AbortSignal) =>
  api("get", "/api/channels/{handle}/videos", {
    params: { handle },
    query: { cursor, limit: CHANNEL_VIDEOS_PAGE_SIZE },
    signal,
  });

/** Плейлисты канала — только открытые (свои все — fetchMyPlaylists). */
export const fetchChannelPlaylists = (handle: string, cursor: string | null, signal?: AbortSignal) =>
  api("get", "/api/channels/{handle}/playlists", {
    params: { handle },
    query: { cursor, limit: PLAYLISTS_PAGE_SIZE },
    signal,
  });

/** Свои плейлисты с видимостью; с videoId — признак contains_video (меню «Сохранить»). */
export const fetchMyPlaylists = (cursor: string | null, videoId?: string, signal?: AbortSignal) =>
  api("get", "/api/me/playlists", { query: { cursor, limit: PLAYLISTS_PAGE_SIZE, video_id: videoId ?? null }, signal });

export const fetchPlaylist = (id: string, signal?: AbortSignal) =>
  api("get", "/api/playlists/{playlist_id}", { ...pl(id), signal });

export const fetchPlaylistVideos = (id: string, cursor: string | null, signal?: AbortSignal) =>
  api("get", "/api/playlists/{playlist_id}/videos", { ...pl(id), query: { cursor, limit: PLAYLISTS_PAGE_SIZE }, signal });

export const createPlaylist = (data: PlaylistCreateIn) => api("post", "/api/playlists", { body: data });

export const updatePlaylist = (id: string, data: PlaylistUpdateIn) =>
  api("patch", "/api/playlists/{playlist_id}", { ...pl(id), body: data });

export const deletePlaylist = (id: string) => api("delete", "/api/playlists/{playlist_id}", pl(id));

export const addToPlaylist = (id: string, videoId: string) =>
  api("put", "/api/playlists/{playlist_id}/videos/{video_id}", { params: { playlist_id: id, video_id: videoId } });

export const removeFromPlaylist = (id: string, videoId: string) =>
  api("delete", "/api/playlists/{playlist_id}/videos/{video_id}", { params: { playlist_id: id, video_id: videoId } });
