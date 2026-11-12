// Типы API — из сгенерированного src/api/schema.d.ts (npm run gen:api). Здесь только
// короткие имена для используемых схем.
import type { components } from "./schema";

type Schemas = components["schemas"];

export type Me = Schemas["MeOut"];
export type RegisterIn = Schemas["RegisterIn"];
export type LoginIn = Schemas["LoginIn"];
export type ErrorOut = Schemas["ErrorOut"];
export type VideoListItem = Schemas["VideoListItem"];
export type VideoPage = Schemas["VideoPage"];
export type ChannelBrief = Schemas["ChannelBrief"];
export type VideoCard = Schemas["VideoCardOut"];
export type VideoStream = Schemas["VideoStreamOut"];
export type VideoSource = Schemas["VideoSourceOut"];
export type ReactionKind = Schemas["ReactionKind"];
export type ReactionOut = Schemas["ReactionOut"];
export type Channel = Schemas["ChannelOut"];
export type SubscriptionOut = Schemas["SubscriptionOut"];
export type ReportCategory = Schemas["ReportCategory"];
export type ReportIn = Schemas["ReportIn"];
export type ReportCreatedOut = Schemas["ReportCreatedOut"];
export type Comment = Schemas["CommentOut"];
export type CommentPage = Schemas["CommentPage"];
export type BanOut = Schemas["BanOut"];
export type UploadCreateIn = Schemas["UploadCreateIn"];
export type UploadCreateOut = Schemas["UploadCreateOut"];
export type UploadPartUrl = Schemas["UploadPartUrl"];
export type CompletedPart = Schemas["CompletedPart"];
export type UploadStatusOut = Schemas["UploadStatusOut"];
export type VideoVisibility = Schemas["VideoVisibility"];
export type VideoStatus = Schemas["VideoStatus"];
export type MyVideoItem = Schemas["MyVideoItem"];
export type MyVideoPage = Schemas["MyVideoPage"];
export type VideoUpdateIn = Schemas["VideoUpdateIn"];
export type AccessEntry = Schemas["AccessEntryOut"];
export type AccessList = Schemas["AccessListOut"];
