import { api } from "./typed";
import type { VideoUpdateIn } from "./types";

export const MY_VIDEOS_PAGE_SIZE = 24;
export const myVideosKey = ["my-videos"] as const;
export const accessKey = (videoId: string) => ["video-access", videoId] as const;

const p = (video_id: string) => ({ params: { video_id } });

export const fetchMyVideos = (cursor: string | null, signal?: AbortSignal) =>
  api("get", "/api/me/videos", { query: { cursor, limit: MY_VIDEOS_PAGE_SIZE }, signal });

/** Правка владельцем: только изменённые поля. */
export const updateVideo = (id: string, data: VideoUpdateIn) =>
  api("patch", "/api/videos/{video_id}", { ...p(id), body: data });

export const deleteVideo = (id: string) => api("delete", "/api/videos/{video_id}", p(id));

export const fetchAccess = (id: string, signal?: AbortSignal) =>
  api("get", "/api/videos/{video_id}/access", { ...p(id), signal });

export const grantAccess = (id: string, username: string) =>
  api("post", "/api/videos/{video_id}/access", { ...p(id), body: { username } });

export const revokeAccess = (id: string, userId: string) =>
  api("delete", "/api/videos/{video_id}/access/{user_id}", { params: { video_id: id, user_id: userId } });
